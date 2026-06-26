#!/usr/bin/env python3
"""Read-only Telegram interactive dashboard for BinanceBot."""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from config_loader import load_config, load_dotenv
import capital_manager


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
CONFIG = load_config(require_api=False)
OFFSET_FILE = os.path.join(BASE_DIR, 'telegram_offset.json')


def _env():
    load_dotenv()
    return (
        os.environ.get('TELEGRAM_BOT_TOKEN', '').strip(),
        os.environ.get('TELEGRAM_CHAT_ID', '').strip(),
    )


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


def _fmt_price(value):
    try:
        return f'{float(value):.4f}'
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


def _state():
    data = _read_json(CONFIG.state_file, {}) or {}
    return data if isinstance(data, dict) else {}


def _positions():
    positions = _state().get('positions')
    return positions if isinstance(positions, list) else []


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
    return 'ONLINE' if (_is_recent(CONFIG.decision_snapshots_file, 15 * 60) or _is_recent(CONFIG.analytics_file, 15 * 60)) else 'OFFLINE'


def _guardian_status():
    return 'ONLINE' if _is_recent(CONFIG.state_file, 15 * 60) else 'OFFLINE'


def _status_icon(status):
    return '🟢' if status in {'ONLINE', 'OK'} else '🟡' if status == 'WARNING' else '🔴'


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
    return value if value and value != 'N/A' else 'N/A'


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
        health, _, _ = _health_summary()
        bot = _bot_status()
        guardian = _guardian_status()
        return '\n'.join([
            '🤖 BinanceBot',
            '',
            f'{_status_icon(bot)} Bot: {bot}',
            f'{_status_icon(guardian)} Guardian: {guardian}',
            '',
            f'💰 Capital: {_fmt_money(state.get("daily_start_capital"))}',
            f'📂 Posiciones abiertas: {len(_positions())}',
            f'📈 PnL hoy: {_fmt_pnl(state.get("daily_pnl_usdt", 0))}',
            '',
            f'🕒 Última ejecución: {_mtime_short(CONFIG.state_file)}',
            f'❤️ Healthcheck: {health}',
            '',
            '────────────',
            '',
            'Seleccione una opción:',
        ])

    def keyboard(self):
        return [
            [_button('💰 Capital', 'capital'), _button('📂 Posiciones', 'positions')],
            [_button('📈 Trades', 'trades'), _button('❤️ Salud', 'health')],
            [_button('📸 Snapshots', 'snapshots'), _button('⚙ Sistema', 'system')],
            [_button('🔄 Actualizar', 'r:home')],
        ]


class CapitalPage(MenuPage):
    page_id = 'capital'

    def render(self):
        try:
            limits = capital_manager.get_limits()
        except Exception as exc:
            missing = str(exc).replace('Missing required environment variable:', '').strip()
            return '\n'.join([
                '💰 Capital',
                '',
                '⚠️ Configuración incompleta',
                f'Falta: {missing or exc}',
                '',
                '────────────',
            ])
        return '\n'.join([
            '💰 Capital',
            '',
            '💵 Spot',
            'Real: N/A',
            f'Límite: {_fmt_money(limits.spot_capital_limit_usdt)}',
            'Disponible: N/A',
            '',
            '📈 Futures',
            'Real: N/A',
            f'Límite: {_fmt_money(limits.futures_capital_limit_usdt)}',
            'Disponible: N/A',
            '',
            '📊 Configuración',
            f'Máx exposición: {limits.max_exposure_percent:.2f}%',
            f'Máx posición: {limits.max_position_percent:.2f}%',
            '',
            '────────────',
        ])


class PositionsPage(MenuPage):
    page_id = 'positions'

    def render(self):
        positions = _positions()
        lines = ['📂 Posiciones abiertas', '']
        if not positions:
            lines.append('✅ No existen posiciones abiertas.')
        for pos in positions[:8]:
            direction = str(pos.get('direction', '')).upper()
            icon = '🟢' if direction == 'LONG' else '🔴'
            lines.extend([
                f'{icon} {pos.get("symbol")} {direction}',
                f'Entrada: {_fmt_price(pos.get("entry_price"))}',
                f'Cantidad: {_fmt(pos.get("quantity"))}',
                f'PnL: {_fmt_money(pos.get("unrealized_pnl"))}',
                '',
            ])
        lines.extend(['', '────────────'])
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
        lines.extend(['', f'Última verificación: {_mtime_short(CONFIG.state_file)}', '', '────────────'])
        return '\n'.join(lines)


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
                f'{icon} {trade.get("symbol")} {trade.get("side")} | '
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
                f'{icon} {regime} | 🎯 {len(candidates)} | '
                f'✅ {counts["accepted"]} | 🚫 {counts["rejected"]} | ⏭ {counts["skipped"]}'
            )
        lines.append('────────────')
        return '\n'.join(lines)


class SystemPage(MenuPage):
    page_id = 'system'

    def render(self):
        bot = _bot_status()
        guardian = _guardian_status()
        dashboard = 'ONLINE' if _is_recent(CONFIG.state_file, 15 * 60) else 'N/A'
        dashboard_since = _systemd_active_since('binancebot-dashboard.service')
        telegram_since = _systemd_active_since('binancebot-telegram.service')
        return '\n'.join([
            '⚙ Sistema',
            '',
            f'{_status_icon(bot)} Bot: {bot}',
            f'{_status_icon(guardian)} Guardian: {guardian}',
            f'Dashboard: {dashboard}',
            f'Versión: {_version()}',
            f'Commit: {_git_commit()}',
            f'Deploy: {_git_deploy_time()}',
            f'Dashboard desde: {dashboard_since}',
            f'Telegram desde: {telegram_since}',
            f'Servidor uptime: {_server_uptime()}',
            '',
            '────────────',
        ])


MENU_PAGES = {
    'home': HomePage(),
    'capital': CapitalPage(),
    'positions': PositionsPage(),
    'health': HealthPage(),
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
