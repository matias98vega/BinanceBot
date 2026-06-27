#!/usr/bin/env python3
"""Read-only Telegram interactive dashboard for BinanceBot."""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from config_loader import ENV_FILES, load_config, load_dotenv
import capital_manager
import bot_state as bot_state_module


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
CONFIG = load_config(require_api=False)
OFFSET_FILE = os.path.join(BASE_DIR, 'telegram_offset.json')
BOT_STATE_FILE = os.path.join(BASE_DIR, 'bot_state.json')
UY_TZ = timezone(timedelta(hours=-3), 'UY')


def _env():
    load_dotenv()
    return (
        os.environ.get('TELEGRAM_BOT_TOKEN', '').strip(),
        os.environ.get('TELEGRAM_CHAT_ID', '').strip(),
    )


def _env_diagnostic():
    load_dotenv()
    detected = [path for path in ENV_FILES if os.path.exists(path)]
    print('TELEGRAM COMMANDS DIAGNOSTIC')
    print(f'cwd: {os.getcwd()}')
    print('env files detected:')
    if detected:
        for path in detected:
            print(f'- {path}')
    else:
        print('- none')
    print(f'BOT_TOTAL_CAPITAL_LIMIT_USDT present: {bool(os.environ.get("BOT_TOTAL_CAPITAL_LIMIT_USDT"))}')
    print(f'BOT_SPOT_CAPITAL_LIMIT_USDT present: {bool(os.environ.get("BOT_SPOT_CAPITAL_LIMIT_USDT"))}')
    print(f'TELEGRAM_BOT_TOKEN present: {bool(os.environ.get("TELEGRAM_BOT_TOKEN"))}')
    print(f'TELEGRAM_CHAT_ID present: {bool(os.environ.get("TELEGRAM_CHAT_ID"))}')


def _read_json(path, default=None):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def _read_jsonl(path):
    records = []
    corrupt = 0
    if not os.path.exists(path):
        return records, corrupt
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    corrupt += 1
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except Exception:
        return records, corrupt + 1
    return records, corrupt


def _mtime_iso(path):
    if not os.path.exists(path):
        return None
    return datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _mtime_short(path):
    value = _mtime_iso(path)
    return value.replace('T', ' ') if value else 'N/A'


def _parse_time(value):
    if value in (None, ''):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    text = str(value).strip()
    try:
        if text.isdigit():
            return datetime.fromtimestamp(int(text), timezone.utc)
        return datetime.fromisoformat(text.replace('Z', '+00:00'))
    except Exception:
        return None


def _fmt_uy(value):
    dt = _parse_time(value)
    if not dt:
        return 'N/A'
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(UY_TZ).strftime('%d/%m %H:%M UY')


def _mtime_uy(path):
    if not os.path.exists(path):
        return 'N/A'
    return _fmt_uy(os.path.getmtime(path))


def _is_recent(path, max_age_seconds):
    return os.path.exists(path) and time.time() - os.path.getmtime(path) <= max_age_seconds


def _fmt(value):
    return 'N/A' if value is None or value == '' else str(value)


def _fmt_money(value):
    try:
        return f'{float(value):.2f} USDT'
    except (TypeError, ValueError):
        return 'N/A'


def _fmt_pnl(value):
    try:
        return f'{float(value):+.2f} USDT'
    except (TypeError, ValueError):
        return 'N/A'


def _fmt_pct_plain(value):
    try:
        return f'{float(value):.1f}%'
    except (TypeError, ValueError):
        return 'N/A'


def _fmt_price(value):
    try:
        return f'{float(value):.4f}'
    except (TypeError, ValueError):
        return 'N/A'


def _display_capacity(current, maximum):
    try:
        current_i = int(current)
    except (TypeError, ValueError):
        current_i = 0
    try:
        max_i = int(maximum)
    except (TypeError, ValueError):
        return str(maximum)
    return max(current_i, max_i)


def _public_price(symbol, direction):
    if not symbol:
        return None
    base = CONFIG.futures_base if str(direction).lower() == 'short' else CONFIG.spot_base
    try:
        qs = urllib.parse.urlencode({'symbol': symbol})
        with urllib.request.urlopen(f'{base}/api/v3/ticker/price?{qs}' if 'fapi' not in base else f'{base}/fapi/v1/ticker/price?{qs}', timeout=4) as r:
            data = json.loads(r.read())
        return float(data.get('price'))
    except Exception:
        return None


def _position_view(pos):
    direction = str(pos.get('direction', '')).lower()
    side = direction.upper()
    symbol = pos.get('symbol') or 'N/D'
    icon = '🟢' if direction == 'long' else '🔴'
    entry = _to_float(pos.get('entry_price'))
    qty = _to_float(pos.get('quantity'))
    tp = _to_float(pos.get('tp'))
    sl = _to_float(pos.get('sl'))
    price = _public_price(symbol, direction) or _to_float(pos.get('current_price')) or entry
    duration = _duration_short(pos.get('entry_time'))

    pnl = pnl_pct = tp_dist = sl_dist = tp_gain = sl_loss = None
    if entry and price and qty:
        if direction == 'short':
            pnl = (entry - price) * qty
            pnl_pct = (entry - price) / entry * 100
        else:
            pnl = (price - entry) * qty
            pnl_pct = (price - entry) / entry * 100
    if price and qty and tp is not None and sl is not None:
        if direction == 'short':
            tp_dist = max(0.0, (price - tp) / price * 100)
            sl_dist = max(0.0, (sl - price) / price * 100)
            tp_gain = max(0.0, (price - tp) * qty)
            sl_loss = -abs((sl - price) * qty)
        else:
            tp_dist = max(0.0, (tp - price) / price * 100)
            sl_dist = max(0.0, (price - sl) / price * 100)
            tp_gain = max(0.0, (tp - price) * qty)
            sl_loss = -abs((price - sl) * qty)

    return '\n'.join([
        f'{icon} {symbol} {side}',
        f'⏱ {duration}',
        '',
        'PnL:',
        f'{_fmt_pnl(pnl)} ({_fmt_pct_plain(pnl_pct)})',
        '',
        '🎯 TP',
        _fmt_pct_plain(tp_dist),
        f'({_fmt_pnl(tp_gain)})',
        '',
        '🛑 SL',
        _fmt_pct_plain(sl_dist),
        f'({_fmt_pnl(sl_loss)})',
    ])


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _duration_short(entry_time):
    dt = _parse_time(entry_time)
    if dt is None:
        value = _to_float(entry_time)
        if value is not None:
            dt = datetime.fromtimestamp(value, timezone.utc)
    if dt is None:
        return 'N/A'
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = max(0, int((datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()))
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    return f'{hours}h {minutes}m' if hours else f'{minutes}m'


def _load_offset():
    data = _read_json(OFFSET_FILE, {}) or {}
    try:
        return int(data.get('offset', 0))
    except (TypeError, ValueError):
        return 0


def _save_offset(offset):
    try:
        with open(OFFSET_FILE, 'w', encoding='utf-8') as f:
            json.dump({'offset': int(offset)}, f, separators=(',', ':'))
            f.write('\n')
    except Exception as exc:
        print(f'Telegram offset write failed: {exc}', file=sys.stderr)


def _state():
    data = _read_json(CONFIG.state_file, {}) or {}
    return data if isinstance(data, dict) else {}


def _bot_state():
    data = _read_json(BOT_STATE_FILE, {}) or {}
    return data if isinstance(data, dict) else {}


def _positions():
    positions = _state().get('positions')
    return positions if isinstance(positions, list) else []


def _capital_limits():
    return capital_manager.get_limits()


def _max_longs(spot_total):
    return _config_int('MAX_LONG_POSITIONS', 2)


def _max_shorts(futures_total):
    return _config_int('MAX_SHORT_POSITIONS', 2)


def _config_int(name, default):
    try:
        with open(os.path.join(BASE_DIR, 'config.py'), encoding='utf-8') as f:
            text = f.read()
        match = re.search(rf'^\s*{re.escape(name)}\s*=\s*([0-9]+)', text, re.MULTILINE)
        if match:
            return int(match.group(1))
    except Exception:
        pass
    return default


def _exposure_metrics():
    snapshot = _bot_state()
    capital = snapshot.get('capital') if isinstance(snapshot.get('capital'), dict) else None
    positions_state = snapshot.get('positions') if isinstance(snapshot.get('positions'), dict) else None
    if capital and positions_state:
        long_state = positions_state.get('long') or {}
        short_state = positions_state.get('short') or {}
        spot_target = capital.get('spot_target')
        futures_target = capital.get('futures_target')
        return {
            'long_count': long_state.get('current', 0),
            'short_count': short_state.get('current', 0),
            'max_longs': long_state.get('max', 'N/D'),
            'max_shorts': short_state.get('max', 'N/D'),
            'total_real': capital.get('total_real'),
            'total_limit': capital.get('total_limit'),
            'total_authorized': capital.get('total_authorized'),
            'spot_real': capital.get('spot_real'),
            'spot_target': spot_target,
            'spot_used': capital.get('spot_used'),
            'spot_reserved': capital.get('spot_reserved'),
            'spot_total': spot_target,
            'futures_real': capital.get('futures_real'),
            'futures_target': futures_target,
            'futures_used': capital.get('futures_used'),
            'futures_reserved': capital.get('futures_reserved'),
            'futures_total': futures_target,
            'warning': capital.get('warning'),
            'note': capital.get('note'),
            'rebalance': snapshot.get('rebalance') if isinstance(snapshot.get('rebalance'), dict) else {},
            'max_exposure_percent': capital.get('max_exposure_percent'),
            'max_position_percent': None,
        }
    positions = _positions()
    try:
        limits = _capital_limits()
        spot_total = limits.spot_capital_limit_usdt
        futures_total = limits.futures_capital_limit_usdt
    except Exception:
        spot_total = None
        futures_total = None

    long_positions = [p for p in positions if isinstance(p, dict) and p.get('direction') == 'long']
    short_positions = [p for p in positions if isinstance(p, dict) and p.get('direction') == 'short']
    spot_used = sum(float(p.get('entry_price') or 0) * float(p.get('quantity') or 0) for p in long_positions)
    futures_used = 0.0
    for pos in short_positions:
        leverage = float(pos.get('leverage') or 1)
        if leverage <= 0:
            leverage = 1.0
        futures_used += float(pos.get('entry_price') or 0) * float(pos.get('quantity') or 0) / leverage

    return {
        'long_count': len(long_positions),
        'short_count': len(short_positions),
        'max_longs': 'N/D',
        'max_shorts': 'N/D',
        'total_real': None,
        'total_limit': None,
        'total_authorized': None,
        'spot_real': None,
        'spot_target': spot_total,
        'spot_used': spot_used,
        'spot_reserved': None,
        'spot_total': spot_total,
        'futures_real': None,
        'futures_target': futures_total,
        'futures_used': futures_used,
        'futures_reserved': None,
        'futures_total': futures_total,
        'warning': None,
        'note': None,
        'rebalance': {},
        'max_exposure_percent': _env_number('BOT_MAX_EXPOSURE_PERCENT'),
        'max_position_percent': None,
    }


def _env_number(name):
    load_dotenv()
    try:
        return float(os.environ.get(name, ''))
    except ValueError:
        return None


def _max_longs_diagnostic():
    snapshot = _bot_state()
    capital = snapshot.get('capital') if isinstance(snapshot.get('capital'), dict) else {}
    positions = snapshot.get('positions') if isinstance(snapshot.get('positions'), dict) else {}
    long_state = positions.get('long') if isinstance(positions.get('long'), dict) else {}
    spot_real = capital.get('spot_real')
    spot_target = capital.get('spot_target')
    input_used = spot_target if spot_target is not None else spot_real
    reported_max = long_state.get('max')
    try:
        dynamic_result = _max_longs(float(input_used)) if input_used is not None else None
    except Exception as exc:
        dynamic_result = f'ERROR: {exc}'
    print('MAX LONGS DIAGNOSTIC')
    print(f'total_limit: {capital.get("total_limit")}')
    print(f'total_authorized: {capital.get("total_authorized")}')
    print(f'spot_real: {spot_real}')
    print(f'spot_target: {spot_target}')
    print(f'long_max_input_used: {input_used}')
    print('long_max_function: Telegram reads bot_state positions.long.max without visual override')
    print(f'long_max_fallback_config_result: {dynamic_result}')
    print(f'long_max_current_bot_state: {reported_max}')


def _merged_trades():
    records, corrupt = _read_jsonl(CONFIG.analytics_file)
    trades = {}
    for record in records:
        trade_id = record.get('trade_id')
        if not trade_id:
            continue
        trades.setdefault(trade_id, {}).update({k: v for k, v in record.items() if v is not None})
    return trades, corrupt


def _health_summary():
    state_exists = os.path.exists(CONFIG.state_file)
    state = _state()
    state_valid = state_exists and isinstance(state, dict)
    positions = state.get('positions', []) if isinstance(state.get('positions'), list) else []
    trades, corrupt_trades = _merged_trades()
    snapshots, corrupt_snapshots = _read_jsonl(CONFIG.decision_snapshots_file)
    open_trades = [t for t in trades.values() if t.get('status') == 'OPEN']
    state_ids = {p.get('id') for p in positions if isinstance(p, dict) and p.get('id')}
    analytics_ids = {t.get('trade_id') for t in open_trades if t.get('trade_id')}

    warnings = []
    errors = []
    if not state_valid:
        errors.append('state.json missing or invalid')
    if not os.path.exists(CONFIG.analytics_file):
        errors.append('trade_analytics.jsonl missing')
    if not os.path.exists(CONFIG.decision_snapshots_file):
        errors.append('decision_snapshots.jsonl missing')
    if corrupt_trades or corrupt_snapshots:
        errors.append(f'corrupt JSONL lines: {corrupt_trades + corrupt_snapshots}')
    if state_ids - analytics_ids:
        warnings.append('state positions missing in analytics')
    if analytics_ids - state_ids:
        warnings.append('analytics OPEN trades missing in state')
    if any(isinstance(s, dict) and s.get('candidates') == [] for s in snapshots[-5:]):
        warnings.append('recent snapshots without candidates')

    status = 'OK'
    if errors:
        status = 'ERROR'
    elif warnings:
        status = 'WARNING'
    return status, warnings, errors


def _bot_status():
    return bot_state_module.get_system_statuses().get('bot', 'UNKNOWN')


def _guardian_status():
    return bot_state_module.get_system_statuses().get('guardian', 'UNKNOWN')


def _dashboard_status():
    return bot_state_module.get_system_statuses().get('dashboard', 'UNKNOWN')


def _telegram_service_status():
    return bot_state_module.get_system_statuses().get('telegram', 'UNKNOWN')


def _diagnostics():
    snapshot = _bot_state()
    diagnostics = snapshot.get('diagnostics') if isinstance(snapshot.get('diagnostics'), dict) else {}
    rebalance = snapshot.get('rebalance') if isinstance(snapshot.get('rebalance'), dict) else {}
    return {
        'entries_allowed': diagnostics.get('entries_allowed'),
        'entries_status': diagnostics.get('entries_status') or ('ENABLED' if diagnostics.get('entries_allowed') is True else 'BLOCKED' if diagnostics.get('entries_allowed') is False else 'UNKNOWN'),
        'entries_reason': diagnostics.get('entries_reason') or 'No disponible',
        'long_entries_status': diagnostics.get('long_entries_status') or 'UNKNOWN',
        'long_entries_reason': diagnostics.get('long_entries_reason') or 'No disponible',
        'short_entries_status': diagnostics.get('short_entries_status') or 'UNKNOWN',
        'short_entries_reason': diagnostics.get('short_entries_reason') or 'No disponible',
        'rebalance': rebalance,
        'rebalance_status': rebalance.get('status') or diagnostics.get('rebalance_status') or 'UNKNOWN',
        'rebalance_reason': rebalance.get('reason') or diagnostics.get('rebalance_reason') or 'No disponible',
        'next_expected_action': diagnostics.get('next_expected_action') or 'No disponible',
        'capital_note': diagnostics.get('capital_note') or 'Ninguna',
        'last_warning': diagnostics.get('last_warning') or 'Ninguno',
        'last_error': diagnostics.get('last_error') or 'Ninguno',
    }


def _entries_label(status, allowed=None):
    status = str(status or '').upper()
    if status == 'ENABLED':
        return '\u2705 Habilitadas'
    if status == 'PARTIAL':
        return '\u26a0\ufe0f Parcialmente bloqueadas'
    if status == 'BLOCKED':
        return '\u274c Bloqueadas'
    if allowed is True:
        return '\u2705 Habilitadas'
    if allowed is False:
        return '\u274c Bloqueadas'
    return '\u26aa No disponible'


def _side_label(status):
    status = str(status or '').upper()
    if status == 'ENABLED':
        return '\u2705 Habilitados'
    if status == 'BLOCKED':
        return '\u26d4 Bloqueados'
    if status == 'WAITING':
        return '\u23f3 Esperando capital'
    return '\u26aa No disponible'


def _rebalance_label(status):
    labels = {
        'PENDING': '\u23f3 Pendiente',
        'NOT_REQUIRED': '\u2705 Alineado',
        'IN_PROGRESS': '\U0001F504 En progreso',
        'DONE': '\u2705 Completado',
        'BLOCKED': '\u26d4 Bloqueado',
    }
    return labels.get(str(status or '').upper(), f'\u26aa {status or "UNKNOWN"}')


def _compact_waiting_reason(reason):
    text = str(reason or 'No disponible')
    match = re.search(r'([0-9]+(?:\.[0-9]+)?)\s*/\s*objetivo\s*([0-9]+(?:\.[0-9]+)?)\s*USDT', text, re.IGNORECASE)
    if match:
        return f'{float(match.group(1)):.2f} / {float(match.group(2)):.2f} USDT'
    return text


def _direction_label(direction):
    return {
        'SPOT_TO_FUTURES': 'Spot \u2192 Futures',
        'FUTURES_TO_SPOT': 'Futures \u2192 Spot',
        'NONE': 'Ninguno',
    }.get(str(direction or 'NONE'), str(direction or 'NONE'))


def _status_icon(status):
    if status in {'ONLINE', 'RUNNING', 'OK'}:
        return '\U0001F7E2'
    if status == 'PAUSED':
        return '\u23f8\ufe0f'
    if status == 'WARNING':
        return '\U0001F7E1'
    return '\U0001F534'


def _version():
    for name in ('VERSION', 'version.txt'):
        path = os.path.join(PROJECT_DIR, name)
        if os.path.exists(path):
            try:
                with open(path, encoding='utf-8') as f:
                    return f.read().strip() or 'N/A'
            except Exception:
                return 'N/A'
    return 'N/A'


def _run_local(args):
    try:
        proc = subprocess.run(
            args,
            cwd=PROJECT_DIR,
            text=True,
            capture_output=True,
            timeout=3,
            check=False,
        )
        if proc.returncode == 0:
            return (proc.stdout or '').strip() or 'N/A'
    except Exception:
        pass
    return 'N/A'


def _git_commit():
    return _run_local(['git', 'rev-parse', '--short', 'HEAD'])


def _git_deploy_time():
    value = _run_local(['git', 'log', '-1', '--format=%ci'])
    if value == 'N/A':
        return value
    try:
        dt = datetime.strptime(value, '%Y-%m-%d %H:%M:%S %z')
        return dt.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    except Exception:
        return value


def _systemd_active_since(service):
    value = _run_local(['systemctl', 'show', service, '--property=ActiveEnterTimestamp', '--value'])
    if not value or value == 'N/A':
        return 'N/A'
    try:
        clean = ' '.join(value.split()[1:])
        dt = datetime.strptime(clean, '%Y-%m-%d %H:%M:%S %Z').replace(tzinfo=timezone.utc)
        return _fmt_uy(dt)
    except Exception:
        return value


def _server_uptime():
    try:
        with open('/proc/uptime', encoding='utf-8') as f:
            seconds = float(f.read().split()[0])
        days = int(seconds // 86400)
        hours = int((seconds % 86400) // 3600)
        minutes = int((seconds % 3600) // 60)
        if days:
            return f'{days}d {hours}h {minutes}m'
        if hours:
            return f'{hours}h {minutes}m'
        return f'{minutes}m'
    except Exception:
        return 'N/A'


def _button(text, data):
    return {'text': text, 'callback_data': data}


def _nav_keyboard(page_id):
    if page_id == 'home':
        return [[_button('🔄 Actualizar', 'r:home')]]
    return [[_button('◀ Menú', 'home')], [_button('🔄 Actualizar', f'r:{page_id}')]]


class MenuPage:
    page_id = ''

    def title(self):
        return ''

    def render(self):
        return self.title()

    def keyboard(self):
        return _nav_keyboard(self.page_id)


class HomePage(MenuPage):
    page_id = 'home'

    def render(self):
        state = _state()
        snapshot = _bot_state()
        system = snapshot.get('system') if isinstance(snapshot.get('system'), dict) else {}
        pnl = snapshot.get('pnl') if isinstance(snapshot.get('pnl'), dict) else {}
        health, _, _ = _health_summary()
        if system.get('health'):
            health = system.get('health')
        bot = _bot_status()
        guardian = _guardian_status()
        metrics = _exposure_metrics()
        max_longs = _display_capacity(metrics["long_count"], metrics["max_longs"])
        max_shorts = _display_capacity(metrics["short_count"], metrics["max_shorts"])
        lines = [
            f'{_status_icon(bot)} Bot: {bot}',
            f'{_status_icon(guardian)} Guardian: {guardian}',
            f'\u2764\ufe0f Healthcheck: {health}',
            '',
            f'PnL hoy: {_fmt_pnl(pnl.get("today", state.get("daily_pnl_usdt", 0)))}',
            f'PnL total: {_fmt_pnl(pnl.get("total", state.get("total_pnl_usdt", 0)))}',
            '',
            f'Longs: {metrics["long_count"]}/{max_longs}',
            f'Spot: {_fmt_money(metrics["spot_used"])} / {_fmt_money(metrics["spot_target"])}',
            '',
            f'Shorts: {metrics["short_count"]}/{max_shorts}',
            f'Futures: {_fmt_money(metrics["futures_used"])} / {_fmt_money(metrics["futures_target"])}',
            '',
            '\U0001F552 Ultimo ciclo',
            _fmt_uy(system.get('last_execution')) if system.get('last_execution') else _mtime_uy(CONFIG.state_file),
        ]
        return '\n'.join(lines)

    def keyboard(self):
        return [
            [_button('\U0001F4B0 Capital', 'capital'), _button('\U0001F4C2 Posiciones', 'positions')],
            [_button('\U0001F4C8 Trades', 'trades'), _button('\u2764\ufe0f Salud', 'health')],
            [_button('\U0001FA7A Diagnostico', 'diagnostics'), _button('\U0001F4F8 Snapshots', 'snapshots')],
            [_button('\u2699 Sistema', 'system')],
            [_button('\U0001F504 Actualizar', 'r:home')],
        ]


class CapitalPage(MenuPage):
    page_id = 'capital'

    def render(self):
        metrics = _exposure_metrics()
        max_exposure = metrics.get('max_exposure_percent')
        max_position = metrics.get('max_position_percent')
        rebalance = metrics.get('rebalance') or {}
        direction_label = _direction_label(rebalance.get('direction'))
        lines = [
            '\U0001F4B0 Capital',
            '',
            'Total:',
            f'Real: {_fmt_money(metrics["total_real"])}',
            f'Limite: {_fmt_money(metrics["total_limit"])}',
            f'Autorizado: {_fmt_money(metrics["total_authorized"])}',
            '',
            'Spot:',
            f'Real: {_fmt_money(metrics["spot_real"])}',
            f'Objetivo: {_fmt_money(metrics["spot_target"])}',
            f'Usado: {_fmt_money(metrics["spot_used"])}',
        ]
        if metrics.get('spot_reserved'):
            lines.append(f'Reserva: {_fmt_money(metrics.get("spot_reserved"))}')
        lines.extend([
            '',
            'Futures:',
            f'Real: {_fmt_money(metrics["futures_real"])}',
            f'Objetivo: {_fmt_money(metrics["futures_target"])}',
            f'Usado: {_fmt_money(metrics["futures_used"])}',
        ])
        if metrics.get('futures_reserved'):
            lines.append(f'Reserva: {_fmt_money(metrics.get("futures_reserved"))}')
        if rebalance:
            lines.extend([
                '',
                'Rebalance:',
                f'{_rebalance_label(rebalance.get("status"))} {direction_label}',
                f'Monto: {_fmt_money(rebalance.get("amount_pending"))}',
            ])
        if metrics.get('warning'):
            lines.extend(['', 'Info:', metrics.get('warning')])
        risk_lines = []
        if max_exposure is not None:
            risk_lines.append(f'Max exposicion: {max_exposure:.2f}%')
        if max_position is not None:
            risk_lines.append(f'Max por operacion: {max_position:.2f}%')
        if risk_lines:
            lines.extend(['', '\u2699\ufe0f Riesgo'])
            lines.extend(risk_lines)
        return '\n'.join(lines)


class PositionsPage(MenuPage):
    page_id = 'positions'

    def render(self):
        positions = _positions()
        lines = ['📂 Posiciones abiertas', '']
        if not positions:
            lines.append('✅ No existen posiciones abiertas.')
        for pos in positions[:8]:
            lines.append(_position_view(pos))
            lines.extend(['', '─' * 12, ''])
        return '\n'.join(lines)


class HealthPage(MenuPage):
    page_id = 'health'

    def render(self):
        status, warnings, errors = _health_summary()
        lines = [
            '❤️ Estado del sistema',
            '',
            f'{_status_icon(status)} Healthcheck: {status}',
            '',
            'Warnings:',
        ]
        lines.extend([f'- {w}' for w in warnings[:6]] or ['- none'])
        lines.append('')
        lines.append('Errores:')
        lines.extend([f'- {e}' for e in errors[:6]] or ['- none'])
        lines.extend(['', f'Última verificación: {_mtime_uy(CONFIG.state_file)}', '', '────────────'])
        return '\n'.join(lines)


class DiagnosticsPage(MenuPage):
    page_id = 'diagnostics'

    def render(self):
        diagnostics = _diagnostics()
        metrics = _exposure_metrics()
        rebalance = diagnostics.get('rebalance') or {}
        direction_label = _direction_label(rebalance.get('direction'))
        max_longs = _display_capacity(metrics["long_count"], metrics["max_longs"])
        max_shorts = _display_capacity(metrics["short_count"], metrics["max_shorts"])
        return '\n'.join([
            '\U0001FA7A Diagnostico',
            '',
            'Capacidad',
            '',
            'Spot:',
            f'{_fmt_money(metrics["spot_used"])} / {_fmt_money(metrics["spot_target"])}',
            '',
            'Futures:',
            f'{_fmt_money(metrics["futures_used"])} / {_fmt_money(metrics["futures_target"])}',
            '',
            'Posiciones posibles:',
            '',
            'Longs:',
            f'{metrics["long_count"]}/{max_longs}',
            '',
            'Shorts:',
            f'{metrics["short_count"]}/{max_shorts}',
            '',
            'Entradas:',
            _entries_label(diagnostics.get('entries_status'), diagnostics.get('entries_allowed')),
            diagnostics.get('entries_reason'),
            '',
            'Longs:',
            _side_label(diagnostics.get('long_entries_status')),
            _compact_waiting_reason(diagnostics.get('long_entries_reason')),
            '',
            'Shorts:',
            _side_label(diagnostics.get('short_entries_status')),
            _compact_waiting_reason(diagnostics.get('short_entries_reason')),
            '',
            'Rebalance:',
            _rebalance_label(diagnostics.get('rebalance_status')),
            direction_label,
            f'Pendiente: {_fmt_money(rebalance.get("amount_pending"))}',
            '',
            'Proxima accion:',
            str(diagnostics.get('next_expected_action') or 'N/D').replace('->', '\u2192'),
            '',
            'Info:',
            'Ninguno',
            '',
            'Error:',
            diagnostics.get('last_error'),
            '',
            '\u2500' * 12,
        ])


class TradesPage(MenuPage):
    page_id = 'trades'

    def render(self):
        trades, _ = _merged_trades()
        closed = [t for t in trades.values() if t.get('status') == 'CLOSED']
        closed.sort(key=lambda t: t.get('exit_time') or '', reverse=True)
        lines = ['📈 Últimos trades', '']
        if not closed:
            lines.append('Sin trades cerrados.')
        for trade in closed[:5]:
            try:
                pnl = float(trade.get('pnl_usdt') or 0)
            except (TypeError, ValueError):
                pnl = 0
            icon = '🟢' if pnl >= 0 else '🔴'
            lines.append(
                f'{icon} {_fmt_uy(trade.get("exit_time"))} | {trade.get("symbol")} {trade.get("side")} | '
                f'{_fmt_pnl(trade.get("pnl_usdt"))} | {_fmt(trade.get("exit_reason"))}'
            )
        lines.extend(['', '────────────'])
        return '\n'.join(lines)


class SnapshotsPage(MenuPage):
    page_id = 'snapshots'

    def render(self):
        snapshots, _ = _read_jsonl(CONFIG.decision_snapshots_file)
        lines = ['📸 Últimos snapshots', '']
        if not snapshots:
            lines.append('Sin snapshots.')
        for snapshot in reversed(snapshots[-3:]):
            candidates = snapshot.get('candidates') or []
            counts = {'accepted': 0, 'rejected': 0, 'skipped': 0}
            for candidate in candidates:
                if isinstance(candidate, dict) and candidate.get('decision') in counts:
                    counts[candidate.get('decision')] += 1
            regime = str(snapshot.get('market_regime') or 'N/A').capitalize()
            icon = '🟥' if regime.lower() == 'bearish' else '🟩' if regime.lower() == 'bullish' else '🟨'
            lines.append(
                f'📸 {_fmt_uy(snapshot.get("timestamp"))}\n'
                f'{icon} {regime} | 🎯 {len(candidates)} | '
                f'✅ {counts["accepted"]} | 🚫 {counts["rejected"]} | ⏭ {counts["skipped"]}'
            )
            lines.extend(['', '─' * 12, ''])
        return '\n'.join(lines)


class SystemPage(MenuPage):
    page_id = 'system'

    def render(self):
        bot = _bot_status()
        guardian = _guardian_status()
        dashboard = _dashboard_status()
        telegram = _telegram_service_status()
        dashboard_since = _systemd_active_since('binancebot-dashboard.service')
        telegram_since = _systemd_active_since('binancebot-telegram.service')
        return '\n'.join([
            '\u2699 Sistema',
            '',
            f'{_status_icon(bot)} Bot: {bot}',
            f'{_status_icon(guardian)} Guardian: {guardian}',
            f'{_status_icon(dashboard)} Dashboard: {dashboard}',
            f'{_status_icon(telegram)} Telegram: {telegram}',
            f'Version: {_version()}',
            f'Commit: {_git_commit()}',
            f'Deploy: {_git_deploy_time()}',
            f'Dashboard desde: {dashboard_since}',
            f'Telegram desde: {telegram_since}',
            f'Servidor uptime: {_server_uptime()}',
            '',
            '\u2500' * 12,
        ])


MENU_PAGES = {
    'home': HomePage(),
    'capital': CapitalPage(),
    'positions': PositionsPage(),
    'health': HealthPage(),
    'diagnostics': DiagnosticsPage(),
    'trades': TradesPage(),
    'snapshots': SnapshotsPage(),
    'system': SystemPage(),
}


COMMAND_ALIASES = {
    '/menu': 'home',
    '/status': 'home',
    '/capital': 'capital',
    '/positions': 'positions',
    '/health': 'health',
    '/diagnostics': 'diagnostics',
    '/lasttrades': 'trades',
    '/snapshots': 'snapshots',
}


def command_help():
    return '\n'.join([
        '🤖 BinanceBot',
        '',
        'Utilice:',
        '',
        '/menu',
        '',
        'para abrir el panel interactivo.',
        '',
        'También disponibles:',
        '',
        '/status',
        '/health',
        '/diagnostics',
        '/capital',
        '/positions',
        '/lasttrades',
        '/snapshots',
        '',
        'Todos los comandos son solo lectura.',
    ])


def _render_page(page_id):
    page = MENU_PAGES.get(page_id) or MENU_PAGES['home']
    return {
        'page_id': page.page_id,
        'text': page.render(),
        'reply_markup': {'inline_keyboard': page.keyboard()},
    }


def _dispatch_text(text):
    command = (text or '').strip().split()[0].lower() if text else ''
    if command == '/help':
        return {'text': command_help()}
    page_id = COMMAND_ALIASES.get(command)
    if page_id:
        return _render_page(page_id)
    if command:
        return {'text': 'Use /menu para abrir el panel interactivo o /help para ayuda.'}
    return None


def _dispatch_callback(data):
    data = (data or '').strip()
    if data.startswith('r:'):
        data = data.split(':', 1)[1] or 'home'
    if data == 'refresh':
        data = 'home'
    return _render_page(data if data in MENU_PAGES else 'home')


def _telegram_request(token, method, params=None, timeout=20):
    data = urllib.parse.urlencode(params or {}).encode('utf-8')
    req = urllib.request.Request(
        f'https://api.telegram.org/bot{token}/{method}',
        data=data,
        method='POST',
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode('utf-8')
    return json.loads(body) if body else {}


def _send_message(token, chat_id, response):
    if not response:
        return
    try:
        params = {
            'chat_id': chat_id,
            'text': response.get('text', '')[:3900],
            'disable_web_page_preview': 'true',
        }
        if response.get('reply_markup'):
            params['reply_markup'] = json.dumps(response['reply_markup'], separators=(',', ':'))
        _telegram_request(token, 'sendMessage', params, timeout=8)
    except Exception as exc:
        print(f'Telegram sendMessage failed: {exc}', file=sys.stderr)


def _edit_message(token, chat_id, message_id, response):
    if not response:
        return
    try:
        params = {
            'chat_id': chat_id,
            'message_id': message_id,
            'text': response.get('text', '')[:3900],
            'disable_web_page_preview': 'true',
        }
        if response.get('reply_markup'):
            params['reply_markup'] = json.dumps(response['reply_markup'], separators=(',', ':'))
        _telegram_request(token, 'editMessageText', params, timeout=8)
    except Exception as exc:
        message = str(exc)
        if 'message is not modified' not in message.lower():
            print(f'Telegram editMessageText failed: {exc}', file=sys.stderr)


def _answer_callback_query(token, callback_query_id):
    if not callback_query_id:
        return
    try:
        _telegram_request(token, 'answerCallbackQuery', {'callback_query_id': callback_query_id}, timeout=5)
    except Exception as exc:
        print(f'Telegram answerCallbackQuery failed: {exc}', file=sys.stderr)


def _process_updates(token, allowed_chat_id, once=False):
    offset = _load_offset()
    params = {
        'timeout': 0 if once else 25,
        'limit': 20,
        'allowed_updates': json.dumps(['message', 'edited_message', 'callback_query']),
    }
    if offset:
        params['offset'] = offset
    try:
        payload = _telegram_request(token, 'getUpdates', params, timeout=35)
    except Exception as exc:
        print(f'Telegram getUpdates failed: {exc}', file=sys.stderr)
        return

    if not payload.get('ok'):
        print('Telegram getUpdates returned not ok', file=sys.stderr)
        return

    max_update_id = offset - 1 if offset else 0
    for update in payload.get('result', []):
        update_id = int(update.get('update_id', 0))
        max_update_id = max(max_update_id, update_id)

        callback = update.get('callback_query')
        if callback:
            message = callback.get('message') or {}
            chat = message.get('chat') or {}
            chat_id = str(chat.get('id', ''))
            if chat_id != str(allowed_chat_id):
                continue
            _answer_callback_query(token, callback.get('id'))
            response = _dispatch_callback(callback.get('data'))
            _edit_message(token, chat_id, message.get('message_id'), response)
            _save_offset(update_id + 1)
            continue

        message = update.get('message') or update.get('edited_message') or {}
        chat = message.get('chat') or {}
        chat_id = str(chat.get('id', ''))
        if chat_id != str(allowed_chat_id):
            _save_offset(update_id + 1)
            continue
        response = _dispatch_text(message.get('text') or '')
        _send_message(token, chat_id, response)
        _save_offset(update_id + 1)

    if max_update_id >= 0:
        _save_offset(max_update_id + 1)


def run(once=False):
    if once:
        _env_diagnostic()
        _max_longs_diagnostic()
    token, chat_id = _env()
    if not token or not chat_id:
        print('Telegram commands inactive: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing')
        return 0
    while True:
        _process_updates(token, chat_id, once=once)
        if once:
            break
    return 0


def main():
    parser = argparse.ArgumentParser(description='Read-only Telegram dashboard for BinanceBot.')
    parser.add_argument('--once', action='store_true', help='Poll once and exit.')
    args = parser.parse_args()
    return run(once=args.once)


if __name__ == '__main__':
    raise SystemExit(main())
