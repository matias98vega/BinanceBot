#!/usr/bin/env python3
"""Read-only Telegram command worker for BinanceBot."""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from config_loader import load_config, load_dotenv
import capital_manager


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = load_config(require_api=False)
OFFSET_FILE = os.path.join(BASE_DIR, 'telegram_offset.json')


def _env():
    load_dotenv()
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    chat_id = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
    return token, chat_id


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


def _is_recent(path, max_age_seconds):
    if not os.path.exists(path):
        return False
    return time.time() - os.path.getmtime(path) <= max_age_seconds


def _fmt(value):
    if value is None or value == '':
        return 'N/A'
    return str(value)


def _fmt_money(value):
    try:
        return f'${float(value):.2f}'
    except (TypeError, ValueError):
        return 'N/A'


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
    state = _read_json(CONFIG.state_file, None)
    state_valid = state_exists and isinstance(state, dict)
    positions = state.get('positions', []) if state_valid and isinstance(state.get('positions'), list) else []
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


def command_status():
    state = _read_json(CONFIG.state_file, {}) or {}
    bot_online = _is_recent(CONFIG.decision_snapshots_file, 15 * 60) or _is_recent(CONFIG.analytics_file, 15 * 60)
    guardian_online = _is_recent(CONFIG.state_file, 15 * 60)
    health_status, _, _ = _health_summary()
    return '\n'.join([
        'STATUS',
        f'Bot: {"ONLINE" if bot_online else "OFFLINE"}',
        f'Guardian: {"ONLINE" if guardian_online else "OFFLINE"}',
        f'Ultima ejecucion: {_fmt(state.get("last_update") or _mtime_iso(CONFIG.state_file))}',
        f'Ultimo snapshot: {_fmt(_mtime_iso(CONFIG.decision_snapshots_file))}',
        f'Healthcheck: {health_status}',
    ])


def command_health():
    status, warnings, errors = _health_summary()
    lines = ['HEALTH', f'Status: {status}']
    if warnings:
        lines.append('Warnings:')
        lines.extend(f'- {warning}' for warning in warnings[:8])
    if errors:
        lines.append('Errors:')
        lines.extend(f'- {error}' for error in errors[:8])
    if not warnings and not errors:
        lines.append('Warnings/Errors: none')
    return '\n'.join(lines)


def command_capital():
    state = _read_json(CONFIG.state_file, {}) or {}
    try:
        limits = capital_manager.get_limits()
        lines = [
            'CAPITAL',
            f'Capital actual local: {_fmt_money(state.get("daily_start_capital"))}',
            'Spot real: N/A (no local balance snapshot)',
            'Spot usable: N/A',
            f'Spot limit: {_fmt_money(limits.spot_capital_limit_usdt)}',
            'Futures real: N/A (no local balance snapshot)',
            'Futures usable: N/A',
            f'Futures limit: {_fmt_money(limits.futures_capital_limit_usdt)}',
            f'Max position: {limits.max_position_percent:.2f}%',
            f'Max exposure: {limits.max_exposure_percent:.2f}%',
        ]
    except Exception as exc:
        lines = ['CAPITAL', f'Capital limits error: {exc}']
    return '\n'.join(lines)


def command_positions():
    state = _read_json(CONFIG.state_file, {}) or {}
    positions = state.get('positions') if isinstance(state.get('positions'), list) else []
    lines = ['POSITIONS', f'Open positions: {len(positions)}']
    for pos in positions[:10]:
        if not isinstance(pos, dict):
            continue
        lines.append(
            f'- {pos.get("symbol")} {str(pos.get("direction", "")).upper()} '
            f'entry={_fmt_money(pos.get("entry_price"))} qty={_fmt(pos.get("quantity"))} '
            f'sl={_fmt_money(pos.get("sl"))} tp={_fmt_money(pos.get("tp"))}'
        )
    if not positions:
        lines.append('- none')
    return '\n'.join(lines)


def command_lasttrades():
    trades, _ = _merged_trades()
    closed = [t for t in trades.values() if t.get('status') == 'CLOSED']
    closed.sort(key=lambda t: t.get('exit_time') or '', reverse=True)
    lines = ['LAST TRADES']
    for trade in closed[:5]:
        lines.append(
            f'- {_fmt(trade.get("exit_time"))} {trade.get("symbol")} {trade.get("side")} '
            f'PnL={_fmt_money(trade.get("pnl_usdt"))} reason={_fmt(trade.get("exit_reason"))}'
        )
    if not closed:
        lines.append('- none')
    return '\n'.join(lines)


def command_snapshots():
    snapshots, _ = _read_jsonl(CONFIG.decision_snapshots_file)
    lines = ['SNAPSHOTS']
    for snapshot in reversed(snapshots[-3:]):
        candidates = snapshot.get('candidates') or []
        counts = {'accepted': 0, 'rejected': 0, 'skipped': 0}
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('decision') in counts:
                counts[candidate.get('decision')] += 1
        lines.append(
            f'- {_fmt(snapshot.get("timestamp"))} regime={_fmt(snapshot.get("market_regime"))} '
            f'accepted={counts["accepted"]} rejected={counts["rejected"]} skipped={counts["skipped"]}'
        )
    if not snapshots:
        lines.append('- none')
    return '\n'.join(lines)


def command_help():
    grouped = {}
    for command, meta in COMMANDS.items():
        if not meta.get('show_in_help'):
            continue
        grouped.setdefault(meta['category'], []).append((command, meta['description']))

    lines = ['AYUDA', 'Comandos disponibles:']
    for category in ('Estado', 'Trading', 'Sistema'):
        items = grouped.get(category, [])
        if not items:
            continue
        lines.append('')
        lines.append(f'{category}:')
        for command, description in items:
            lines.append(f'- {command}: {description}')

    lines.extend([
        '',
        'Proximamente:',
        '- /pnl: resumen PnL',
        '- /stats: estadisticas avanzadas',
        '- /logs: ultimos logs',
        '- /version: version del bot',
    ])
    return '\n'.join(lines)


def _menu_keyboard():
    rows = []
    buttons = [
        ('Estado', '/status'),
        ('Health', '/health'),
        ('Capital', '/capital'),
        ('Posiciones', '/positions'),
        ('Ultimos trades', '/lasttrades'),
        ('Snapshots', '/snapshots'),
        ('Ayuda', '/help'),
    ]
    for index in range(0, len(buttons), 2):
        row = [{'text': text, 'callback_data': command} for text, command in buttons[index:index + 2]]
        rows.append(row)
    return {'inline_keyboard': rows}


def command_menu():
    return {
        'text': 'Menu BinanceBot\nElegir una consulta:',
        'reply_markup': _menu_keyboard(),
    }


COMMANDS = {
    '/status': {
        'name': 'status',
        'description': 'estado general del bot',
        'category': 'Estado',
        'handler': command_status,
        'show_in_help': True,
        'show_in_menu': True,
    },
    '/health': {
        'name': 'health',
        'description': 'healthcheck resumido',
        'category': 'Estado',
        'handler': command_health,
        'show_in_help': True,
        'show_in_menu': True,
    },
    '/capital': {
        'name': 'capital',
        'description': 'capital y limites configurados',
        'category': 'Estado',
        'handler': command_capital,
        'show_in_help': True,
        'show_in_menu': True,
    },
    '/positions': {
        'name': 'positions',
        'description': 'posiciones abiertas locales',
        'category': 'Estado',
        'handler': command_positions,
        'show_in_help': True,
        'show_in_menu': True,
    },
    '/lasttrades': {
        'name': 'lasttrades',
        'description': 'ultimos 5 trades cerrados',
        'category': 'Trading',
        'handler': command_lasttrades,
        'show_in_help': True,
        'show_in_menu': True,
    },
    '/snapshots': {
        'name': 'snapshots',
        'description': 'ultimos 3 snapshots de decision',
        'category': 'Trading',
        'handler': command_snapshots,
        'show_in_help': True,
        'show_in_menu': True,
    },
    '/help': {
        'name': 'help',
        'description': 'ayuda y comandos disponibles',
        'category': 'Sistema',
        'handler': command_help,
        'show_in_help': True,
        'show_in_menu': True,
    },
    '/menu': {
        'name': 'menu',
        'description': 'botonera de consultas',
        'category': 'Sistema',
        'handler': command_menu,
        'show_in_help': True,
        'show_in_menu': True,
    },
}


def _normalize_response(response):
    if isinstance(response, dict):
        return response
    return {'text': response}


def _dispatch(text):
    command = (text or '').strip().split()[0].lower() if text else ''
    meta = COMMANDS.get(command)
    if meta:
        return _normalize_response(meta['handler']())
    if command:
        return {'text': 'Comandos disponibles: /help /menu /status /health /capital /positions /lasttrades /snapshots'}
    return None


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


def _send_message(token, chat_id, text, reply_markup=None):
    try:
        params = {
            'chat_id': chat_id,
            'text': text[:3900],
            'disable_web_page_preview': 'true',
        }
        if reply_markup:
            params['reply_markup'] = json.dumps(reply_markup, separators=(',', ':'))
        _telegram_request(token, 'sendMessage', params, timeout=8)
    except Exception as exc:
        print(f'Telegram sendMessage failed: {exc}', file=sys.stderr)


def _answer_callback_query(token, callback_query_id):
    if not callback_query_id:
        return
    try:
        _telegram_request(token, 'answerCallbackQuery', {'callback_query_id': callback_query_id}, timeout=5)
    except Exception as exc:
        print(f'Telegram answerCallbackQuery failed: {exc}', file=sys.stderr)


def _respond(token, chat_id, response):
    if not response:
        return
    _send_message(token, chat_id, response.get('text', ''), response.get('reply_markup'))


def _process_updates(token, allowed_chat_id, once=False):
    offset = _load_offset()
    params = {'timeout': 0 if once else 25}
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
        if update.get('callback_query'):
            callback = update.get('callback_query') or {}
            message = callback.get('message') or {}
            chat = message.get('chat') or {}
            chat_id = str(chat.get('id', ''))
            if chat_id != str(allowed_chat_id):
                continue
            _answer_callback_query(token, callback.get('id'))
            response = _dispatch(callback.get('data') or '')
            _respond(token, chat_id, response)
            continue

        message = update.get('message') or update.get('edited_message') or {}
        chat = message.get('chat') or {}
        chat_id = str(chat.get('id', ''))
        if chat_id != str(allowed_chat_id):
            continue
        text = message.get('text') or ''
        response = _dispatch(text)
        _respond(token, chat_id, response)

    if max_update_id >= 0:
        _save_offset(max_update_id + 1)


def run(once=False):
    token, chat_id = _env()
    if not token or not chat_id:
        print('Telegram commands inactive: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing')
        return 0
    while True:
        _process_updates(token, chat_id, once=once)
        if once:
            break
        time.sleep(1)
    return 0


def main():
    parser = argparse.ArgumentParser(description='Read-only Telegram commands for BinanceBot.')
    parser.add_argument('--once', action='store_true', help='Poll once and exit.')
    args = parser.parse_args()
    return run(once=args.once)


if __name__ == '__main__':
    raise SystemExit(main())
