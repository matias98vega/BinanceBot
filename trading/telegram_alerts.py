#!/usr/bin/env python3
"""Non-blocking Telegram alerts for important bot events."""
import json
import os
import sys
import urllib.parse
import urllib.request

from config_loader import load_dotenv


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


def _level_value(level):
    return LEVELS.get(str(level or 'INFO').upper(), LEVELS['INFO'])


def _configured():
    load_dotenv()
    enabled = _env_bool('TELEGRAM_ALERTS_ENABLED', False)
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    chat_id = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
    min_level = os.environ.get('TELEGRAM_ALERT_LEVEL', 'WARNING').strip().upper() or 'WARNING'
    if min_level not in LEVELS:
        min_level = 'WARNING'
    return enabled, token, chat_id, min_level


def send_telegram_alert(level, title, message):
    """
    Send a Telegram alert if enabled and above threshold.
    Returns True only when Telegram accepts the request. Never raises.
    """
    try:
        enabled, token, chat_id, min_level = _configured()
        level = str(level or 'INFO').upper()
        if level not in LEVELS:
            level = 'INFO'
        if not enabled:
            return False
        if _level_value(level) < _level_value(min_level):
            return False
        if not token or not chat_id:
            print('Telegram alerts enabled but TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing', file=sys.stderr)
            return False

        text = f'[{level}] {title}\n{message}'
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
    except Exception as exc:
        print(f'Telegram alert failed: {exc}', file=sys.stderr)
        return False


def main():
    sent = send_telegram_alert('WARNING', 'Telegram alert test', 'Test message from BinanceBot.')
    enabled, _, chat_id, min_level = _configured()
    print('TELEGRAM ALERT TEST')
    print(f'Enabled: {enabled}')
    print(f'Chat ID configured: {bool(chat_id)}')
    print(f'Min level: {min_level}')
    print(f'Sent: {sent}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
