#!/usr/bin/env python3
"""Local read-only monitoring dashboard for BinanceBot."""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(DASHBOARD_DIR)
TRADING_DIR = os.path.join(PROJECT_DIR, 'trading')
sys.path.insert(0, TRADING_DIR)

from config_loader import load_config  # noqa: E402
import capital_manager  # noqa: E402
import decision_timeline  # noqa: E402
import insights_engine  # noqa: E402
import trade_inspector  # noqa: E402


CONFIG = load_config(require_api=False)
BOT_STATE_FILE = os.path.join(TRADING_DIR, 'bot_state.json')
REBALANCE_STATUS_FILE = os.path.join(PROJECT_DIR, 'data', 'history', 'rebalance_status.json')
HOST = os.environ.get('DASHBOARD_HOST', '127.0.0.1')
PORT = int(os.environ.get('DASHBOARD_PORT', '8080'))


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
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                corrupt += 1
                continue
            if isinstance(data, dict):
                records.append(data)
    return records, corrupt


def _mtime_iso(path):
    if not os.path.exists(path):
        return None
    return datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _age_seconds(path):
    if not os.path.exists(path):
        return None
    return max(0, time.time() - os.path.getmtime(path))


def _is_recent(path, max_age_seconds):
    age = _age_seconds(path)
    return age is not None and age <= max_age_seconds


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt_status(ok):
    return 'ONLINE' if ok else 'OFFLINE'


def _merged_trades():
    records, corrupt = _read_jsonl(CONFIG.analytics_file)
    trades = {}
    for record in records:
        trade_id = record.get('trade_id')
        if not trade_id:
            continue
        trades.setdefault(trade_id, {}).update({k: v for k, v in record.items() if v is not None})
    return trades, corrupt


def _closed_trades():
    trades, _ = _merged_trades()
    return [trade for trade in trades.values() if trade.get('status') == 'CLOSED']


def _profit_factor(trades):
    gross_profit = sum(_float(t.get('pnl_usdt')) for t in trades if _float(t.get('pnl_usdt')) > 0)
    gross_loss = abs(sum(_float(t.get('pnl_usdt')) for t in trades if _float(t.get('pnl_usdt')) < 0))
    if gross_loss == 0:
        return None if gross_profit == 0 else 'inf'
    return round(gross_profit / gross_loss, 4)


def _win_rate(trades):
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if _float(t.get('pnl_usdt')) > 0)
    return round(wins / len(trades) * 100, 4)


def _last_error():
    paths = [CONFIG.trades_log, CONFIG.analysis_log]
    patterns = ('error', 'failed', 'fallo', 'falló', 'exception')
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                lines = f.readlines()[-200:]
        except Exception:
            continue
        for line in reversed(lines):
            if any(pattern in line.lower() for pattern in patterns):
                return line.strip()
    return None


def _status_payload():
    bot_state = _read_json(BOT_STATE_FILE, {}) or {}
    system = bot_state.get('system') if isinstance(bot_state, dict) else {}
    if isinstance(system, dict) and system:
        return {
            'bot': system.get('bot') or 'UNKNOWN',
            'guardian': system.get('guardian') or _fmt_status(_is_recent(CONFIG.state_file, 15 * 60)),
            'last_execution': system.get('last_execution') or _mtime_iso(CONFIG.state_file),
            'last_snapshot': system.get('last_snapshot') or _mtime_iso(CONFIG.decision_snapshots_file),
            'last_healthcheck': system.get('last_healthcheck') or _mtime_iso(CONFIG.state_file),
            'last_preflight': _mtime_iso(CONFIG.cycle_baseline_file),
            'diagnostics': bot_state.get('diagnostics') or {},
            'rebalance': bot_state.get('rebalance') or {},
        }
    state = _read_json(CONFIG.state_file, {}) or {}
    return {
        'bot': _fmt_status(_is_recent(CONFIG.decision_snapshots_file, 15 * 60) or _is_recent(CONFIG.analytics_file, 15 * 60)),
        'guardian': _fmt_status(_is_recent(CONFIG.state_file, 15 * 60)),
        'last_execution': state.get('last_update') or _mtime_iso(CONFIG.state_file),
        'last_snapshot': _mtime_iso(CONFIG.decision_snapshots_file),
        'last_healthcheck': _mtime_iso(CONFIG.state_file),
        'last_preflight': _mtime_iso(CONFIG.cycle_baseline_file),
    }


def _trades_payload(limit=20):
    closed = sorted(_closed_trades(), key=lambda t: t.get('exit_time') or '', reverse=True)
    rows = []
    for trade in closed[:limit]:
        rows.append({
            'time': trade.get('exit_time') or trade.get('entry_time'),
            'symbol': trade.get('symbol'),
            'side': trade.get('side'),
            'result': 'WIN' if _float(trade.get('pnl_usdt')) > 0 else 'LOSS' if _float(trade.get('pnl_usdt')) < 0 else 'FLAT',
            'pnl_usdt': trade.get('pnl_usdt'),
            'exit_reason': trade.get('exit_reason'),
        })
    return rows


def _snapshots_payload(limit=10):
    snapshots, corrupt = _read_jsonl(CONFIG.decision_snapshots_file)
    rows = []
    for snapshot in reversed(snapshots[-limit:]):
        candidates = snapshot.get('candidates') or []
        counts = {'accepted': 0, 'rejected': 0, 'skipped': 0}
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get('decision') in counts:
                counts[candidate.get('decision')] += 1
        rows.append({
            'timestamp': snapshot.get('timestamp'),
            'market_regime': snapshot.get('market_regime'),
            'candidates': len(candidates),
            'accepted': counts['accepted'],
            'rejected': counts['rejected'],
            'skipped': counts['skipped'],
        })
    return {'snapshots': rows, 'corrupt_lines': corrupt}


def _metrics_payload():
    bot_state = _read_json(BOT_STATE_FILE, {}) or {}
    if isinstance(bot_state, dict) and bot_state.get('capital'):
        capital = bot_state.get('capital') or {}
        positions = bot_state.get('positions') or {}
        pnl = bot_state.get('pnl') or {}
        diagnostics = bot_state.get('diagnostics') or {}
        rebalance_state = bot_state.get('rebalance') or {}
        trades, corrupt = _merged_trades()
        closed = [t for t in trades.values() if t.get('status') == 'CLOSED']
        open_now = [t for t in trades.values() if t.get('status') == 'OPEN']
        longs = [t for t in closed if str(t.get('side', '')).upper() == 'LONG']
        shorts = [t for t in closed if str(t.get('side', '')).upper() == 'SHORT']
        return {
            'capital_current': capital.get('total_authorized'),
            'spot_capital_limit_usdt': capital.get('spot_target'),
            'futures_capital_limit_usdt': capital.get('futures_target'),
            'spot_used': capital.get('spot_used'),
            'futures_used': capital.get('futures_used'),
            'total_real': capital.get('total_real'),
            'total_limit': capital.get('total_limit'),
            'capital_warning': capital.get('warning'),
            'capital_note': capital.get('note'),
            'long_positions': (positions.get('long') or {}).get('current'),
            'max_long_positions': (positions.get('long') or {}).get('max'),
            'short_positions': (positions.get('short') or {}).get('current'),
            'max_short_positions': (positions.get('short') or {}).get('max'),
            'pnl_total': pnl.get('total'),
            'win_rate': _win_rate(closed),
            'profit_factor': _profit_factor(closed),
            'open_trades': len(open_now),
            'closed_trades': len(closed),
            'state_open_positions': len(_read_json(CONFIG.state_file, {}).get('positions', []) if isinstance(_read_json(CONFIG.state_file, {}), dict) else []),
            'long_win_rate': _win_rate(longs),
            'short_win_rate': _win_rate(shorts),
            'corrupt_trade_lines': corrupt,
            'diagnostics': diagnostics,
            'rebalance': rebalance_state,
        }
    state = _read_json(CONFIG.state_file, {}) or {}
    positions = state.get('positions') if isinstance(state.get('positions'), list) else []
    trades, corrupt = _merged_trades()
    closed = [t for t in trades.values() if t.get('status') == 'CLOSED']
    open_now = [t for t in trades.values() if t.get('status') == 'OPEN']
    longs = [t for t in closed if str(t.get('side', '')).upper() == 'LONG']
    shorts = [t for t in closed if str(t.get('side', '')).upper() == 'SHORT']
    try:
        cap = capital_manager.snapshot()
    except Exception:
        cap = {}
    return {
        'capital_current': state.get('daily_start_capital'),
        'spot_capital_limit_usdt': cap.get('spot_capital_limit_usdt'),
        'futures_capital_limit_usdt': cap.get('futures_capital_limit_usdt'),
        'max_exposure_percent': cap.get('max_exposure_percent'),
        'pnl_total': state.get('total_pnl_usdt'),
        'win_rate': _win_rate(closed),
        'profit_factor': _profit_factor(closed),
        'open_trades': len(open_now),
        'closed_trades': len(closed),
        'state_open_positions': len(positions),
        'long_win_rate': _win_rate(longs),
        'short_win_rate': _win_rate(shorts),
        'corrupt_trade_lines': corrupt,
    }


def _health_payload():
    state = _read_json(CONFIG.state_file, {}) or {}
    positions = state.get('positions') if isinstance(state.get('positions'), list) else []
    trades, corrupt_trades = _merged_trades()
    snapshots, corrupt_snapshots = _read_jsonl(CONFIG.decision_snapshots_file)
    state_ids = {p.get('id') for p in positions if isinstance(p, dict) and p.get('id')}
    open_ids = {t.get('trade_id') for t in trades.values() if t.get('status') == 'OPEN' and t.get('trade_id')}
    aligned = not (state_ids - open_ids) and not (open_ids - state_ids)
    observability = 'OK'
    if corrupt_trades or corrupt_snapshots:
        observability = 'ERROR'
    elif any(snapshot.get('candidates') == [] for snapshot in snapshots):
        observability = 'WARNING'
    return {
        'healthcheck': 'OK' if aligned else 'WARNING',
        'observability': observability,
        'json_corrupt_lines': corrupt_trades + corrupt_snapshots,
        'state_alignment': aligned,
        'last_error': _last_error(),
    }


def _timeline_payload(limit=20):
    return {'events': decision_timeline.read_recent_events(limit=limit)}


def _rebalance_payload():
    data = _read_json(REBALANCE_STATUS_FILE, {}) or {}
    return data if isinstance(data, dict) else {}


def _insights_payload():
    return insights_engine.load_insights()


def _trade_payload(trade_id):
    return trade_inspector.inspect_trade(trade_id=trade_id)


def _api_payload(path):
    if path == '/api/status':
        return _status_payload()
    if path == '/api/trades':
        return {'trades': _trades_payload()}
    if path == '/api/snapshots':
        return _snapshots_payload()
    if path == '/api/health':
        return _health_payload()
    if path == '/api/metrics':
        return _metrics_payload()
    if path == '/api/timeline':
        return _timeline_payload()
    if path == '/api/rebalance':
        return _rebalance_payload()
    if path == '/api/insights':
        return _insights_payload()
    if path.startswith('/api/trade/'):
        trade_id = path.rsplit('/', 1)[-1]
        return _trade_payload(trade_id)
    return None


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DASHBOARD_DIR, **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        payload = _api_payload(parsed.path)
        if payload is not None:
            self._send_json(payload)
            return
        if parsed.path in {'/', '/index.html'}:
            self.path = '/templates/index.html'
        return super().do_GET()

    def _send_json(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write('%s - %s\n' % (self.log_date_time_string(), fmt % args))


def main():
    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)
    print(f'Dashboard running at http://{HOST}:{PORT}')
    server.serve_forever()


if __name__ == '__main__':
    main()
