#!/usr/bin/env python3
"""Passive reconciliation for Futures positions observed on Binance."""

import json
import logging
import os
import time
from datetime import datetime, timezone


TRADING_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TRADING_DIR)
HISTORY_DIR = os.path.join(PROJECT_DIR, 'data', 'history')
DEFAULT_STATUS_FILE = os.path.join(HISTORY_DIR, 'futures_reconciliation_status.json')
DEFAULT_TRADES_FILE = os.path.join(HISTORY_DIR, 'trades.jsonl')
ALERT_THROTTLE_SECONDS = 6 * 3600


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _safe_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except Exception:
        return None


def _read_json(path, default=None):
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else (default or {})
    except FileNotFoundError:
        return default or {}
    except Exception as exc:
        logging.warning('futures reconciliation status read failed path=%s error=%s', path, exc)
        return default or {}


def _write_json(path, payload):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f'{path}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write('\n')
        os.replace(tmp, path)
    except Exception as exc:
        logging.warning('futures reconciliation status write failed path=%s error=%s', path, exc)


def load_status(path=DEFAULT_STATUS_FILE):
    return _read_json(path, {})


def _iter_jsonl(path):
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if isinstance(record, dict):
                    yield record
    except FileNotFoundError:
        return
    except Exception as exc:
        logging.warning('futures reconciliation history read failed path=%s error=%s', path, exc)


def _history_index(trades_file=DEFAULT_TRADES_FILE):
    by_symbol = {}
    for record in _iter_jsonl(trades_file):
        symbol = str(record.get('symbol') or '').upper()
        side = str(record.get('side') or record.get('direction') or '').upper()
        if not symbol or side != 'SHORT':
            continue
        bucket = by_symbol.setdefault(symbol, {'known': False, 'has_closed': False, 'last_opened_at': None, 'last_closed_at': None})
        bucket['known'] = True
        event_type = str(record.get('event_type') or '').upper()
        status = str(record.get('status') or '').upper()
        if event_type == 'TRADE_OPEN' or status == 'OPEN':
            bucket['last_opened_at'] = record.get('opened_at') or record.get('recorded_at') or bucket.get('last_opened_at')
        if event_type == 'TRADE_CLOSE' or status == 'CLOSED':
            bucket['has_closed'] = True
            bucket['last_closed_at'] = record.get('closed_at') or record.get('recorded_at') or bucket.get('last_closed_at')
    return by_symbol


def _state_short_index(state):
    state = state if isinstance(state, dict) else {}
    result = {}
    positions = state.get('positions') if isinstance(state.get('positions'), list) else []
    for pos in positions:
        if not isinstance(pos, dict) or str(pos.get('direction') or '').lower() != 'short':
            continue
        symbol = str(pos.get('symbol') or '').upper()
        if symbol:
            result[symbol] = pos
    return result


def _has_lifecycle_metadata(pos):
    if not isinstance(pos, dict):
        return False
    return bool(pos.get('entry_price') and pos.get('quantity') and (pos.get('id') or pos.get('trade_id') or pos.get('entry_time')))


def _normalise_position(raw):
    raw = raw if isinstance(raw, dict) else {}
    amount = _safe_float(raw.get('positionAmt'), _safe_float(raw.get('position_amt')))
    if amount is None:
        quantity = _safe_float(raw.get('quantity'))
        side_hint = str(raw.get('side') or raw.get('direction') or '').upper()
        if quantity is not None:
            amount = -abs(quantity) if side_hint == 'SHORT' else abs(quantity)
    amount = amount or 0.0
    side = str(raw.get('side') or '').upper()
    if not side:
        side = 'SHORT' if amount < 0 else 'LONG' if amount > 0 else 'UNKNOWN'
    notional = _safe_float(raw.get('notional'))
    return {
        'symbol': str(raw.get('symbol') or '').upper(),
        'side': side,
        'position_amt': amount,
        'notional': None if notional is None else abs(notional),
        'entry_price': _safe_float(raw.get('entry_price'), _safe_float(raw.get('entryPrice'))),
        'mark_price': _safe_float(raw.get('mark_price'), _safe_float(raw.get('markPrice'))),
        'unrealized_pnl': _safe_float(raw.get('unrealized_pnl'), _safe_float(raw.get('unRealizedProfit'), _safe_float(raw.get('unrealizedProfit')))),
        'leverage': _safe_float(raw.get('leverage')),
        'margin_type': raw.get('margin_type') or raw.get('marginType'),
        'position_margin': _safe_float(raw.get('position_margin'), _safe_float(raw.get('positionInitialMargin'), _safe_float(raw.get('initialMargin')))),
        'isolated_margin': _safe_float(raw.get('isolatedMargin'), _safe_float(raw.get('isolated_margin'))),
        'liquidation_price': _safe_float(raw.get('liquidationPrice'), _safe_float(raw.get('liquidation_price'))),
    }


def _severity(classification):
    if 'desynced_closed_but_open_on_exchange' in classification or 'unprotected_futures_position' in classification:
        return 'ERROR'
    if 'unmanaged_futures_position' in classification:
        return 'WARNING'
    return 'INFO'


def classify_positions(observed_positions, state=None, open_orders_by_symbol=None, trades_file=DEFAULT_TRADES_FILE):
    observed_positions = observed_positions if isinstance(observed_positions, list) else []
    open_orders_by_symbol = open_orders_by_symbol if isinstance(open_orders_by_symbol, dict) else {}
    state_shorts = _state_short_index(state)
    history = _history_index(trades_file)
    now = _now_iso()
    positions = {}

    for raw in observed_positions:
        pos = _normalise_position(raw)
        symbol = pos.get('symbol')
        if not symbol or (_safe_float(pos.get('position_amt'), 0.0) or 0.0) == 0:
            continue
        state_pos = state_shorts.get(symbol)
        hist = history.get(symbol, {})
        orders = open_orders_by_symbol.get(symbol)
        has_open_orders = bool(orders) if isinstance(orders, list) else bool(orders)
        classification = ['observed_futures_position']
        managed = state_pos is not None and _has_lifecycle_metadata(state_pos)
        if managed:
            classification.append('managed_futures_position')
        else:
            classification.extend(['unmanaged_futures_position', 'orphan_futures_position'])
        if not has_open_orders:
            classification.append('unprotected_futures_position')
        if hist.get('has_closed'):
            classification.append('desynced_closed_but_open_on_exchange')
        opened_at = hist.get('last_opened_at') or (state_pos or {}).get('entry_time')
        opened_ts = _parse_ts(opened_at)
        age_hours = None if opened_ts is None else round(max(0, time.time() - opened_ts) / 3600, 2)
        if age_hours is not None and age_hours >= 24:
            classification.append('stale_observed_futures_position')
        positions[symbol] = {
            'symbol': symbol,
            'side': pos.get('side'),
            'position_amt': pos.get('position_amt'),
            'notional': pos.get('notional'),
            'entry_price': pos.get('entry_price'),
            'mark_price': pos.get('mark_price'),
            'unrealized_pnl': pos.get('unrealized_pnl'),
            'leverage': pos.get('leverage'),
            'margin_type': pos.get('margin_type'),
            'position_margin': pos.get('position_margin'),
            'isolated_margin': pos.get('isolated_margin'),
            'liquidation_price': pos.get('liquidation_price'),
            'has_open_orders': has_open_orders,
            'open_orders_count': len(orders) if isinstance(orders, list) else (1 if orders else 0),
            'managed_in_state': managed,
            'known_in_history': bool(hist.get('known')),
            'classification': classification,
            'suspected_opened_at': opened_at,
            'age_hours': age_hours,
            'last_seen': now,
            'severity': _severity(classification),
            'suggested_action': 'manual review or explicit recovery close',
        }
    return positions


def _summary(positions, allowed_count=None, position_margin_total=None):
    values = list((positions or {}).values())
    orphan_count = sum(1 for p in values if 'orphan_futures_position' in (p.get('classification') or []))
    unmanaged_count = sum(1 for p in values if 'unmanaged_futures_position' in (p.get('classification') or []))
    unprotected_count = sum(1 for p in values if 'unprotected_futures_position' in (p.get('classification') or []))
    desynced_count = sum(1 for p in values if 'desynced_closed_but_open_on_exchange' in (p.get('classification') or []))
    observed_count = len(values)
    allowed_value = None
    try:
        allowed_value = int(allowed_count) if allowed_count is not None else None
    except (TypeError, ValueError):
        allowed_value = None
    aligned = (
        (allowed_value is None or observed_count <= allowed_value)
        and unmanaged_count == 0
        and orphan_count == 0
        and unprotected_count == 0
        and desynced_count == 0
    )
    if aligned:
        status = 'ALINEADO'
    else:
        reasons = []
        if allowed_value is not None and observed_count > allowed_value:
            reasons.append('EXCESO FUTURES')
        if unmanaged_count:
            reasons.append('RIESGO NO GESTIONADAS')
        if not reasons:
            reasons.append('NO ALINEADO')
        status = ' / '.join(reasons)
    position_margin = round(sum(_safe_float(p.get('position_margin'), 0.0) or 0.0 for p in values), 8)
    if position_margin <= 0 and position_margin_total is not None:
        position_margin = _safe_float(position_margin_total, 0.0) or 0.0
    return {
        'observed_count': len(values),
        'managed_count': sum(1 for p in values if p.get('managed_in_state')),
        'unmanaged_count': unmanaged_count,
        'orphan_count': orphan_count,
        'unprotected_count': unprotected_count,
        'desynced_count': desynced_count,
        'allowed_count': allowed_value,
        'aligned': aligned,
        'status': status,
        'position_margin': round(position_margin, 8),
        'notional': round(sum(abs(_safe_float(p.get('notional'), 0.0) or 0.0) for p in values), 8),
    }


def _alert_message(entry):
    return (
        '🚨 Futures desincronizadas detectadas\n\n'
        f'{entry.get("symbol")} {entry.get("side")}\n'
        f'Estado: {_human_status(entry)}\n'
        f'Cantidad: {entry.get("position_amt")}\n'
        f'Notional: {abs(_safe_float(entry.get("notional"), 0.0) or 0.0):.2f} USDT\n'
        f'PnL no realizado: {(_safe_float(entry.get("unrealized_pnl"), 0.0) or 0.0):+.2f} USDT\n'
        f'Ordenes abiertas: {"si" if entry.get("has_open_orders") else "ninguna"}\n'
        'Riesgo: sin TP/SL/reduce-only\n\n'
        'Accion:\nRequiere revision manual o recovery explicito.'
    )


def _human_status(entry):
    classes = entry.get('classification') or []
    if 'desynced_closed_but_open_on_exchange' in classes:
        return 'cerrada en historial, abierta en Binance'
    if 'unmanaged_futures_position' in classes:
        return 'observada en Binance, no gestionada en state'
    return 'observada en Binance'


def persist_reconciliation(positions, status_file=DEFAULT_STATUS_FILE, alert_fn=None, allowed_count=None,
                           position_margin_total=None):
    previous = load_status(status_file)
    previous_positions = previous.get('positions') if isinstance(previous.get('positions'), dict) else {}
    now = _now_iso()
    for symbol, entry in positions.items():
        prior = previous_positions.get(symbol) if isinstance(previous_positions.get(symbol), dict) else {}
        last_alert = prior.get('last_alert')
        should_alert = entry.get('severity') in {'ERROR', 'CRITICAL'} and (
            _parse_ts(last_alert) is None or time.time() - _parse_ts(last_alert) >= ALERT_THROTTLE_SECONDS
        )
        entry['first_seen'] = prior.get('first_seen') or now
        entry['last_alert'] = now if should_alert else prior.get('last_alert')
        entry['alert_count'] = int(prior.get('alert_count') or 0) + (1 if should_alert else 0)
        if should_alert and alert_fn:
            try:
                alert_fn(_alert_message(entry))
            except Exception:
                pass
    payload = {
        'updated_at': now,
        'summary': _summary(positions, allowed_count=allowed_count, position_margin_total=position_margin_total),
        'positions': positions,
    }
    summary = payload['summary']
    logging.warning(
        'FUTURES RECONCILIATION summary observed=%s managed=%s unmanaged=%s orphan=%s '
        'unprotected=%s desynced=%s allowed=%s status=%s',
        summary.get('observed_count'),
        summary.get('managed_count'),
        summary.get('unmanaged_count'),
        summary.get('orphan_count'),
        summary.get('unprotected_count'),
        summary.get('desynced_count'),
        summary.get('allowed_count'),
        summary.get('status'),
    )
    _write_json(status_file, payload)
    return payload


def reconcile_observed_positions(observed_positions, state=None, open_orders_by_symbol=None,
                                 trades_file=DEFAULT_TRADES_FILE, status_file=DEFAULT_STATUS_FILE,
                                 alert_fn=None, allowed_count=None, position_margin_total=None):
    positions = classify_positions(observed_positions, state=state, open_orders_by_symbol=open_orders_by_symbol, trades_file=trades_file)
    return persist_reconciliation(
        positions,
        status_file=status_file,
        alert_fn=alert_fn,
        allowed_count=allowed_count,
        position_margin_total=position_margin_total,
    )


def collect_open_orders(binance, observed_positions):
    result = {}
    for raw in observed_positions or []:
        symbol = str((raw or {}).get('symbol') or '').upper()
        if not symbol:
            continue
        try:
            orders = binance.futures_open_orders({'symbol': symbol})
        except Exception as exc:
            logging.warning('futures reconciliation open orders read failed symbol=%s error=%s', symbol, exc)
            orders = None
        result[symbol] = orders if isinstance(orders, list) else []
    return result


def reconciliation_summary_from_status(status=None):
    status = status if isinstance(status, dict) else load_status()
    summary = status.get('summary') if isinstance(status.get('summary'), dict) else {}
    return summary
