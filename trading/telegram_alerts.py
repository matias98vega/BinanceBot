#!/usr/bin/env python3
"""Non-blocking Telegram alerts for important bot events."""
import json
import hashlib
import os
import sys
import time
import urllib.parse
import urllib.request

from atomic_persistence import atomic_write_text
from config_loader import PROJECT_DIR, load_dotenv
from notification_guard import external_notifications_disabled, log_suppressed


ALERT_STATE_FILE = os.path.join(PROJECT_DIR, 'trading', 'telegram_alert_state.json')
DEFAULT_COOLDOWN_SECONDS = 1800
DEFAULT_NOTIFY_FLAGS = {
    'OPEN': True,
    'CLOSE': True,
    'TP': True,
    'SL': True,
    'PARTIAL': True,
    'REBALANCE': True,
    'BLACKLIST': False,
    'SNAPSHOT': False,
    'WARNING': True,
    'ERROR': True,
    'CRITICAL': True,
}

LEVELS = {
    'INFO': 10,
    'WARNING': 20,
    'ERROR': 30,
    'CRITICAL': 40,
}


def _env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None or raw == '':
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'y', 'on'}


def notification_enabled(kind, level=None):
    load_dotenv()
    key = str(kind or level or 'INFO').strip().upper()
    if key in {'INFO'}:
        return True
    if key not in DEFAULT_NOTIFY_FLAGS and level:
        key = str(level).strip().upper()
    default = DEFAULT_NOTIFY_FLAGS.get(key, True)
    return _env_bool(f'TELEGRAM_NOTIFY_{key}', default)


def _level_value(level):
    return LEVELS.get(str(level or 'INFO').upper(), LEVELS['INFO'])


def _env_int(name, default):
    raw = os.environ.get(name)
    if raw is None or raw == '':
        return default
    try:
        value = int(float(raw))
    except ValueError:
        return default
    return value if value > 0 else default


def _configured():
    load_dotenv()
    enabled = _env_bool('TELEGRAM_ALERTS_ENABLED', False)
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    chat_id = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
    min_level = os.environ.get('TELEGRAM_ALERT_LEVEL', 'WARNING').strip().upper() or 'WARNING'
    cooldown = _env_int('TELEGRAM_ALERT_COOLDOWN_SECONDS', DEFAULT_COOLDOWN_SECONDS)
    if min_level not in LEVELS:
        min_level = 'WARNING'
    return enabled, token, chat_id, min_level, cooldown


def _normalize_text(value):
    return ' '.join(str(value or '').strip().split())


def _normalize_event_key(value):
    return _normalize_text(value).lower()


def _fingerprint(level, title, message, event_key=None):
    identity = f'event:{_normalize_event_key(event_key)}' if event_key else _normalize_text(message)
    raw = f'{_normalize_text(level).upper()}|{_normalize_text(title)}|{identity}'
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _read_state():
    try:
        with open(ALERT_STATE_FILE, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(f'Telegram alert state read failed: {exc}', file=sys.stderr)
        return {}


def _write_state(state):
    try:
        os.makedirs(os.path.dirname(ALERT_STATE_FILE), exist_ok=True)
        serialized = json.dumps(state, separators=(',', ':'), ensure_ascii=False) + '\n'
        atomic_write_text(ALERT_STATE_FILE, serialized, mode=0o600)
        try:
            os.chmod(ALERT_STATE_FILE, 0o600)
        except Exception:
            pass
        return True
    except Exception as exc:
        print(f'Telegram alert state write failed: {exc}', file=sys.stderr)
        return False


def _cooldown_suppressed(level, title, message, cooldown):
    if level == 'CRITICAL':
        return False, None
    fp = _fingerprint(level, title, message)
    state = _read_state()
    alerts = state.get('alerts') if isinstance(state.get('alerts'), dict) else {}
    previous = alerts.get(fp) if isinstance(alerts.get(fp), dict) else {}
    last_sent = float(previous.get('last_sent', 0) or 0)
    if last_sent and time.time() - last_sent < cooldown:
        return True, fp
    return False, fp


def _event_active(event_key):
    state = _read_state()
    events = state.get('event_conditions') if isinstance(state.get('event_conditions'), dict) else {}
    previous = events.get(_normalize_event_key(event_key))
    return bool(isinstance(previous, dict) and previous.get('active') is True)


def rearm_alert_event(event_key):
    """Persist ACTIVE -> INACTIVE for one semantic alert episode."""
    event_key = _normalize_event_key(event_key)
    if not event_key:
        return False
    state = _read_state()
    events = state.get('event_conditions') if isinstance(state.get('event_conditions'), dict) else {}
    previous = events.get(event_key)
    if not isinstance(previous, dict) or previous.get('active') is not True:
        return False
    now = time.time()
    events[event_key] = {
        **previous,
        'active': False,
        'rearmed_at': now,
    }
    state['event_conditions'] = events
    state['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))
    persisted = _write_state(state)
    if persisted:
        print(f'Telegram alert event rearmed: event_key={event_key}', file=sys.stderr)
    return persisted


def _record_sent(level, title, message, fingerprint, event_key=None):
    if (level == 'CRITICAL' and not event_key) or not fingerprint:
        return False
    state = _read_state()
    alerts = state.get('alerts') if isinstance(state.get('alerts'), dict) else {}
    now = time.time()
    alerts[fingerprint] = {
        'last_sent': now,
        'level': level,
        'title': _normalize_text(title),
        'message': _normalize_text(message)[:500],
    }
    state['alerts'] = alerts
    event_key = _normalize_event_key(event_key)
    if event_key:
        events = state.get('event_conditions') if isinstance(state.get('event_conditions'), dict) else {}
        events[event_key] = {
            'active': True,
            'activated_at': now,
            'last_sent': now,
            'level': level,
            'title': _normalize_text(title),
            'message': _normalize_text(message)[:500],
            'fingerprint': fingerprint,
        }
        state['event_conditions'] = events
    state['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))
    return _write_state(state)


def _format_alert(level, title, message):
    icons = {
        'INFO': '\u2139\ufe0f',
        'WARNING': '\u26a0\ufe0f',
        'ERROR': '\U0001F6A8',
        'CRITICAL': '\U0001F6D1',
    }
    heading = 'BinanceBot'
    icon = icons.get(level, '\u26a0\ufe0f')
    body = message if not title or title == 'BinanceBot' else f'{title}\n\n{message}'
    return f'{icon} {heading}\nNivel: {level}\n\n{body}'

def _send_raw(token, chat_id, text):
    data = urllib.parse.urlencode({
        'chat_id': chat_id,
        'text': text[:3900],
        'disable_web_page_preview': 'true',
    }).encode('utf-8')
    req = urllib.request.Request(
        f'https://api.telegram.org/bot{token}/sendMessage',
        data=data,
        method='POST',
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        body = response.read().decode('utf-8')
    payload = json.loads(body) if body else {}
    return bool(payload.get('ok'))


def send_telegram_alert(level, title, message, notification_type=None, event_key=None):
    """
    Send a Telegram alert if enabled and above threshold.
    Returns True only when Telegram accepts the request. Never raises.
    """
    try:
        if external_notifications_disabled():
            log_suppressed('telegram')
            return False
        enabled, token, chat_id, min_level, cooldown = _configured()
        level = str(level or 'INFO').upper()
        if level not in LEVELS:
            level = 'INFO'
        if not enabled:
            return False
        if not notification_enabled(notification_type, level):
            return False
        if _level_value(level) < _level_value(min_level):
            return False
        if not token or not chat_id:
            print('Telegram alerts enabled but TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing', file=sys.stderr)
            return False

        event_key = _normalize_event_key(event_key)
        if event_key:
            fp = _fingerprint(level, title, message, event_key=event_key)
            if _event_active(event_key):
                print(
                    f'Telegram alert suppressed: event_key={event_key} reason=active_episode',
                    file=sys.stderr,
                )
                return False
        else:
            suppressed, fp = _cooldown_suppressed(level, title, message, cooldown)
            if suppressed:
                return False

        sent = _send_raw(token, chat_id, _format_alert(level, title, message))
        if sent:
            persisted = _record_sent(level, title, message, fp, event_key=event_key)
            if event_key:
                print(
                    f'Telegram alert sent: event_key={event_key} '
                    f'reason=episode_activated state_persisted={str(bool(persisted)).lower()}',
                    file=sys.stderr,
                )
        return sent
    except Exception as exc:
        print(f'Telegram alert failed: {exc}', file=sys.stderr)
        return False


def main():
    sent = send_telegram_alert('WARNING', 'Telegram alert test', 'Test message from BinanceBot.')
    enabled, _, chat_id, min_level, cooldown = _configured()
    print('TELEGRAM ALERT TEST')
    print(f'Enabled: {enabled}')
    print(f'Chat ID configured: {bool(chat_id)}')
    print(f'Min level: {min_level}')
    print(f'Cooldown seconds: {cooldown}')
    print(f'Sent: {sent}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

