#!/usr/bin/env python3
"""Passive reconstruction of a trade from historical observability files."""
import json
import logging
import os
from datetime import datetime, timezone

import analytics_engine
import decision_timeline
import history


NOT_AVAILABLE = 'Dato no disponible'
IMPORTANT_CATEGORIES = {
    'SIGNAL', 'SIZING', 'RISK', 'REBALANCE', 'ORDER', 'PROTECTION',
    'GUARDIAN', 'CAPITAL',
}
IMPORTANT_EVENT_KEYWORDS = (
    'signal', 'sizing', 'rebalance', 'order', 'opened', 'closed', 'tp',
    'sl', 'oco', 'guardian', 'recovery', 'protect', 'stale', 'trailing',
)


def _iter_jsonl(path):
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict):
                    yield data
    except Exception as exc:
        logging.warning('trade_inspector read failed path=%s error=%s', path, exc)
        return


def _parse_dt(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except Exception:
            return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(timezone.utc)
    except Exception:
        return None


def _iso(value):
    dt = _parse_dt(value)
    return dt.replace(microsecond=0).isoformat().replace('+00:00', 'Z') if dt else None


def _float(value):
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _duration(opened_at, closed_at):
    start = _parse_dt(opened_at)
    end = _parse_dt(closed_at)
    if not start or not end:
        return None, NOT_AVAILABLE
    seconds = max(0, int((end - start).total_seconds()))
    hours, rem = divmod(seconds, 3600)
    minutes, _ = divmod(rem, 60)
    if hours:
        return seconds, f'{hours}h {minutes}m'
    return seconds, f'{minutes}m'


def _record_time(record):
    for key in ('timestamp', 'recorded_at', 'opened_at', 'closed_at', 'entry_time', 'exit_time'):
        value = record.get(key)
        if _parse_dt(value):
            return value
    return None


def _merge_trade_events(trades_file):
    trades = {}
    events = {}
    for record in _iter_jsonl(trades_file) or []:
        trade_id = record.get('trade_id')
        if not trade_id:
            continue
        events.setdefault(trade_id, []).append(record)
        merged = trades.setdefault(trade_id, {})
        merged.update({k: v for k, v in record.items() if v is not None})
    return trades, events


def _select_trade_by_id(trade_id, trades):
    return trades.get(trade_id)


def _select_trade_by_symbol_date(symbol, near, trades):
    symbol = str(symbol or '').upper()
    target = _parse_dt(near)
    candidates = [trade for trade in trades.values() if str(trade.get('symbol') or '').upper() == symbol]
    if not candidates:
        return None
    if not target:
        return sorted(candidates, key=lambda t: _parse_dt(t.get('opened_at') or t.get('closed_at')) or datetime.min.replace(tzinfo=timezone.utc))[-1]
    return min(
        candidates,
        key=lambda t: abs(((_parse_dt(t.get('opened_at') or t.get('closed_at')) or target) - target).total_seconds()),
    )


def _latest_trade(trades, result=None):
    candidates = list(trades.values())
    if result == 'WIN':
        candidates = [t for t in candidates if str(t.get('result') or '').upper() == 'WIN' or (_float(t.get('pnl_usdt')) or 0) > 0]
    elif result == 'LOSS':
        candidates = [t for t in candidates if str(t.get('result') or '').upper() == 'LOSS' or (_float(t.get('pnl_usdt')) or 0) < 0]
    if not candidates:
        return None
    return sorted(candidates, key=lambda t: _parse_dt(t.get('closed_at') or t.get('opened_at')) or datetime.min.replace(tzinfo=timezone.utc))[-1]


def _window_match(record, trade, minutes_before=180, minutes_after=180):
    opened = _parse_dt(trade.get('opened_at'))
    closed = _parse_dt(trade.get('closed_at')) or opened
    ts = _parse_dt(_record_time(record))
    if not ts or not opened:
        return False
    start = opened.timestamp() - minutes_before * 60
    end = (closed or opened).timestamp() + minutes_after * 60
    return start <= ts.timestamp() <= end


def _related(record, trade):
    trade_id = trade.get('trade_id')
    symbol = str(trade.get('symbol') or '').upper()
    side = str(trade.get('side') or trade.get('direction') or '').upper()
    if trade_id and record.get('related_trade_id') == trade_id:
        return True
    if trade_id and record.get('trade_id') == trade_id:
        return True
    rec_symbol = str(record.get('symbol') or '').upper()
    rec_side = str(record.get('side') or record.get('direction') or '').upper()
    if rec_symbol and rec_symbol == symbol and (not rec_side or not side or rec_side == side):
        return _window_match(record, trade)
    return False


def _find_decisions(trade, decisions_file):
    return [record for record in (_iter_jsonl(decisions_file) or []) if _related(record, trade)]


def _find_snapshots(trade, snapshots_file):
    symbol = str(trade.get('symbol') or '').upper()
    matches = []
    for snapshot in _iter_jsonl(snapshots_file) or []:
        if not _window_match(snapshot, trade, minutes_before=360, minutes_after=60):
            continue
        candidates = snapshot.get('candidates') or []
        if any(str(c.get('symbol') or '').upper() == symbol for c in candidates if isinstance(c, dict)):
            matches.append(snapshot)
        elif not matches:
            matches.append(snapshot)
    return matches


def _important_timeline(events):
    selected = []
    for event in events:
        category = str(event.get('category') or '').upper()
        name = str(event.get('event') or '').lower()
        message = str(event.get('message') or '').lower()
        if category in IMPORTANT_CATEGORIES or any(k in name or k in message for k in IMPORTANT_EVENT_KEYWORDS):
            selected.append({
                'timestamp': event.get('timestamp') or NOT_AVAILABLE,
                'level': event.get('level') or 'INFO',
                'category': event.get('category') or NOT_AVAILABLE,
                'event': event.get('event') or NOT_AVAILABLE,
                'message': event.get('message') or NOT_AVAILABLE,
                'details': event.get('details') or {},
            })
    selected.sort(key=lambda item: item.get('timestamp') or '')
    return selected


def _find_timeline(trade, timeline_file):
    events = [record for record in (_iter_jsonl(timeline_file) or []) if _related(record, trade)]
    return _important_timeline(events)


def _first_present(*values):
    for value in values:
        if value not in (None, '', {}, []):
            return value
    return None


def _extract_candidate(trade, snapshots):
    symbol = str(trade.get('symbol') or '').upper()
    for snapshot in snapshots:
        for candidate in snapshot.get('candidates') or []:
            if isinstance(candidate, dict) and str(candidate.get('symbol') or '').upper() == symbol:
                return candidate
    return {}


def _extract_market(trade, decisions, snapshots):
    candidate = _extract_candidate(trade, snapshots)
    decision = decisions[-1] if decisions else {}
    btc_context = _first_present(trade.get('btc_context'), decision.get('btc_context'), candidate.get('btc_context'))
    market_regime = _first_present(trade.get('market_regime'), decision.get('market_regime'))
    if not market_regime:
        for snapshot in snapshots:
            market_regime = _first_present(snapshot.get('market_regime'), (snapshot.get('market') or {}).get('regime'))
            if market_regime:
                break
    btc_price = None
    if isinstance(btc_context, dict):
        btc_price = _first_present(btc_context.get('btc_price'), btc_context.get('price'))
    return {
        'regime': market_regime or NOT_AVAILABLE,
        'btc_context': btc_context or NOT_AVAILABLE,
        'btc_price': btc_price if btc_price is not None else NOT_AVAILABLE,
        'score': _first_present(trade.get('score'), decision.get('score'), candidate.get('score')) or NOT_AVAILABLE,
        'entry_reasons': _first_present(candidate.get('reasons'), decision.get('steps'), decision.get('reason')) or NOT_AVAILABLE,
    }


def _extract_capital(trade, snapshots, timeline):
    capital = {}
    exposure = {}
    rebalance = []
    for snapshot in snapshots:
        if not capital and isinstance(snapshot.get('capital'), dict):
            capital = snapshot.get('capital') or {}
        if not exposure and isinstance(snapshot.get('exposure'), dict):
            exposure = snapshot.get('exposure') or {}
    for event in timeline:
        if event.get('category') == 'REBALANCE':
            rebalance.append(event)
    return {
        'available': _first_present(capital.get('available'), capital.get('total_real'), capital.get('total_authorized')) or NOT_AVAILABLE,
        'used': _first_present(trade.get('capital_used'), capital.get('used'), exposure.get('used')) or NOT_AVAILABLE,
        'exposure': exposure or NOT_AVAILABLE,
        'rebalance_applied': bool(rebalance),
        'rebalance_events': rebalance,
    }


def _extract_protections(trade, timeline):
    events_text = ' '.join(f'{e.get("event")} {e.get("message")}'.lower() for e in timeline)
    return {
        'tp': _first_present(trade.get('tp'), 'detectado' if 'tp' in events_text else None) or NOT_AVAILABLE,
        'sl': _first_present(trade.get('sl'), 'detectado' if 'sl' in events_text else None) or NOT_AVAILABLE,
        'oco': 'detectado' if 'oco' in events_text else NOT_AVAILABLE,
        'guardian': 'detectado' if 'guardian' in events_text else NOT_AVAILABLE,
        'recovery': 'detectado' if 'recovery' in events_text else NOT_AVAILABLE,
    }


def _summary(trade):
    opened_at = trade.get('opened_at')
    closed_at = trade.get('closed_at')
    duration_seconds, duration_text = _duration(opened_at, closed_at)
    return {
        'trade_id': trade.get('trade_id') or NOT_AVAILABLE,
        'symbol': trade.get('symbol') or NOT_AVAILABLE,
        'direction': trade.get('side') or trade.get('direction') or NOT_AVAILABLE,
        'opened_at': _iso(opened_at) or NOT_AVAILABLE,
        'closed_at': _iso(closed_at) or NOT_AVAILABLE,
        'duration_seconds': duration_seconds if duration_seconds is not None else NOT_AVAILABLE,
        'duration': duration_text,
        'pnl_usdt': trade.get('pnl_usdt') if trade.get('pnl_usdt') is not None else NOT_AVAILABLE,
        'pnl_pct': trade.get('pnl_pct') if trade.get('pnl_pct') is not None else NOT_AVAILABLE,
        'exit_reason': trade.get('exit_reason') or NOT_AVAILABLE,
        'status': trade.get('status') or NOT_AVAILABLE,
        'result': trade.get('result') or NOT_AVAILABLE,
    }


def _conclusion(summary, market, protections, timeline):
    pnl = _float(summary.get('pnl_usdt'))
    exit_reason = str(summary.get('exit_reason') or '').upper()
    direction = str(summary.get('direction') or '').upper()
    regime = str(market.get('regime') or '').lower()
    text = 'Trade reconstruido con informacion historica parcial.'
    if pnl is not None and pnl > 0:
        if exit_reason == 'TP':
            text = 'Trade ganador con TP alcanzado.'
        else:
            text = f'Trade ganador cerrado por {exit_reason or "motivo no disponible"}.'
    elif pnl is not None and pnl < 0:
        if protections.get('guardian') != NOT_AVAILABLE:
            text = 'Trade perdedor con intervencion del Guardian.'
        elif exit_reason == 'SL':
            text = 'Trade cerrado por SL.'
        else:
            text = f'Trade perdedor cerrado por {exit_reason or "motivo no disponible"}.'
    if direction == 'SHORT' and 'bear' in regime and pnl is not None and pnl > 0:
        text = 'Trade ganador siguiendo tendencia bajista.'
        if exit_reason == 'TP' and protections.get('guardian') == NOT_AVAILABLE:
            text += ' TP alcanzado sin intervencion del Guardian.'
    if protections.get('recovery') != NOT_AVAILABLE:
        text += ' Requirio recovery automatico.'
    if any('sizing_rejected' == e.get('event') for e in timeline):
        text = 'Trade rechazado por sizing.'
    if any('sl_native_failed' == e.get('event') for e in timeline) and 'guardian' in str(protections.get('guardian')).lower():
        text = 'Trade cerrado por SL software tras fallo de proteccion nativa.'
    return {
        'text': text,
        'rules': {
            'pnl_usdt': pnl,
            'exit_reason': exit_reason or NOT_AVAILABLE,
            'direction': direction or NOT_AVAILABLE,
            'regime': market.get('regime') or NOT_AVAILABLE,
            'recovery_detected': protections.get('recovery') != NOT_AVAILABLE,
            'guardian_detected': protections.get('guardian') != NOT_AVAILABLE,
        },
    }


def _stats_context(trade, stats_file):
    try:
        stats = analytics_engine.load_stats(stats_file=stats_file, rebuild_if_missing=False)
        symbol = trade.get('symbol')
        side = str(trade.get('side') or '').upper()
        return {
            'symbol_stats': (stats.get('by_symbol') or {}).get(symbol, {}),
            'direction_stats': (stats.get('by_direction') or {}).get(side, {}),
        }
    except Exception:
        return {'symbol_stats': {}, 'direction_stats': {}}


def inspect_trade(
    trade_id=None,
    symbol=None,
    near=None,
    trades_file=history.DEFAULT_TRADES_FILE,
    decisions_file=history.DEFAULT_DECISIONS_FILE,
    snapshots_file=history.DEFAULT_SNAPSHOTS_FILE,
    timeline_file=decision_timeline.DEFAULT_TIMELINE_FILE,
    stats_file=analytics_engine.DEFAULT_STATS_FILE,
):
    try:
        trades, trade_events = _merge_trade_events(trades_file)
        trade = _select_trade_by_id(trade_id, trades) if trade_id else _select_trade_by_symbol_date(symbol, near, trades)
        if not trade:
            return {
                'found': False,
                'error': 'Trade no encontrado',
                'summary': {'trade_id': trade_id or NOT_AVAILABLE, 'symbol': symbol or NOT_AVAILABLE},
            }
        decisions = _find_decisions(trade, decisions_file)
        snapshots = _find_snapshots(trade, snapshots_file)
        timeline = _find_timeline(trade, timeline_file)
        summary = _summary(trade)
        market = _extract_market(trade, decisions, snapshots)
        capital = _extract_capital(trade, snapshots, timeline)
        protections = _extract_protections(trade, timeline)
        return {
            'found': True,
            'summary': summary,
            'market': market,
            'timeline': timeline,
            'capital': capital,
            'protections': protections,
            'conclusion': _conclusion(summary, market, protections, timeline),
            'analytics_context': _stats_context(trade, stats_file),
            'raw_refs': {
                'trade_events': trade_events.get(trade.get('trade_id'), []),
                'decision_count': len(decisions),
                'snapshot_count': len(snapshots),
                'timeline_count': len(timeline),
            },
        }
    except Exception as exc:
        logging.warning('trade_inspector failed: %s', exc)
        return {'found': False, 'error': str(exc), 'summary': {'trade_id': trade_id or NOT_AVAILABLE}}


def inspect_latest(result=None, **kwargs):
    trades, _ = _merge_trade_events(kwargs.get('trades_file', history.DEFAULT_TRADES_FILE))
    trade = _latest_trade(trades, result=result)
    if not trade:
        return {'found': False, 'error': 'Trade no encontrado'}
    kwargs.pop('trade_id', None)
    return inspect_trade(trade_id=trade.get('trade_id'), **kwargs)


def list_recent_trades(limit=5, trades_file=history.DEFAULT_TRADES_FILE):
    trades, _ = _merge_trade_events(trades_file)
    rows = sorted(
        trades.values(),
        key=lambda t: _parse_dt(t.get('closed_at') or t.get('opened_at')) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return [_summary(trade) for trade in rows[:max(1, int(limit or 5))]]


def format_for_telegram(report):
    if not report or not report.get('found'):
        return '🔍 Trade Inspector\n\nTrade no encontrado.'
    summary = report.get('summary') or {}
    market = report.get('market') or {}
    protections = report.get('protections') or {}
    conclusion = report.get('conclusion') or {}
    timeline = report.get('timeline') or []
    lines = [
        '🔍 Trade Inspector',
        '',
        f'{summary.get("symbol", NOT_AVAILABLE)} {summary.get("direction", NOT_AVAILABLE)}',
        f'PnL: {summary.get("pnl_usdt", NOT_AVAILABLE)} USDT ({summary.get("pnl_pct", NOT_AVAILABLE)}%)',
        f'Salida: {summary.get("exit_reason", NOT_AVAILABLE)}',
        f'Duracion: {summary.get("duration", NOT_AVAILABLE)}',
        '',
        'Mercado:',
        f'Regimen: {market.get("regime", NOT_AVAILABLE)}',
        f'Score: {market.get("score", NOT_AVAILABLE)}',
        '',
        'Proteccion:',
        f'TP: {protections.get("tp", NOT_AVAILABLE)} | SL: {protections.get("sl", NOT_AVAILABLE)}',
        f'OCO: {protections.get("oco", NOT_AVAILABLE)} | Guardian: {protections.get("guardian", NOT_AVAILABLE)}',
        '',
        'Conclusion:',
        conclusion.get('text', NOT_AVAILABLE),
    ]
    if timeline:
        lines.extend(['', 'Timeline:'])
        for event in timeline[:6]:
            lines.append(f'{event.get("category")} | {event.get("message")}')
    return '\n'.join(str(line) for line in lines)
