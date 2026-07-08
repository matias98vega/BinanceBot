#!/usr/bin/env python3
"""Defensive handling for managed Futures residuals left without protection."""

import json
import logging
import os
from datetime import datetime, timezone

import config
import decision_timeline
import futures_reconciliation
import utils
import version_history


TRADING_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TRADING_DIR)
REPORT_DIR = os.path.join(PROJECT_DIR, 'data', 'history', 'repair_reports')
POSITION_ZERO_TOLERANCE = 1e-12


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def _safe_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _position_from_rows(rows, symbol):
    symbol = str(symbol or '').upper()
    if isinstance(rows, dict):
        rows = [rows]
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if str(row.get('symbol') or '').upper() != symbol:
            continue
        amount = _safe_float(row.get('positionAmt'), _safe_float(row.get('position_amt'), 0.0)) or 0.0
        if abs(amount) <= POSITION_ZERO_TOLERANCE:
            return {
                'symbol': symbol,
                'position_amt': 0.0,
                'notional': 0.0,
                'raw': row,
            }
        return {
            'symbol': symbol,
            'position_amt': amount,
            'notional': abs(_safe_float(row.get('notional'), 0.0) or 0.0),
            'mark_price': _safe_float(row.get('markPrice'), _safe_float(row.get('mark_price'))),
            'entry_price': _safe_float(row.get('entryPrice'), _safe_float(row.get('entry_price'))),
            'raw': row,
        }
    return {'symbol': symbol, 'position_amt': 0.0, 'notional': 0.0, 'raw': None}


def refresh_position(client, symbol):
    return _position_from_rows(client.futures_position_risk({'symbol': symbol}), symbol)


def refresh_open_orders(client, symbol):
    orders = client.futures_open_orders({'symbol': symbol})
    return orders if isinstance(orders, list) else []


def _close_side(position_amt):
    return 'BUY' if position_amt < 0 else 'SELL'


def _round_quantity(client, symbol, quantity):
    filters = client.get_futures_filters(symbol)
    filters = filters if isinstance(filters, dict) else {}
    step = _safe_float(filters.get('step_size'), 0.0) or 0.0
    min_qty = _safe_float(filters.get('min_qty'), 0.0) or 0.0
    rounded = utils.round_step(quantity, step) if step else quantity
    if rounded <= 0 or (min_qty > 0 and rounded < min_qty):
        return None, {'reason': 'unclosable_by_min_qty', 'quantity': quantity, 'rounded_quantity': rounded, 'min_qty': min_qty, 'step_size': step}
    return rounded, {'min_qty': min_qty, 'step_size': step}


def _is_reduce_only(order):
    value = order.get('reduceOnly') if isinstance(order, dict) else None
    return str(value).lower() == 'true' or value is True


def cancel_reduce_only_orders(client, symbol, orders):
    cancelled = []
    for order in orders or []:
        if not isinstance(order, dict) or not _is_reduce_only(order):
            continue
        order_id = order.get('orderId')
        if order_id in (None, ''):
            continue
        try:
            client.cancel_futures_order({'symbol': symbol, 'orderId': order_id})
            cancelled.append(order_id)
        except Exception as exc:
            logging.warning('FUTURES RESIDUAL cancel reduceOnly failed symbol=%s orderId=%s error=%s', symbol, order_id, exc)
    return cancelled


def _remove_state_position(state, symbol):
    positions = state.get('positions') if isinstance(state.get('positions'), list) else []
    before = len(positions)
    state['positions'] = [p for p in positions if str(p.get('symbol') or '').upper() != symbol]
    return before - len(state['positions'])


def remove_state_position(state, symbol):
    return _remove_state_position(state, symbol)


def _write_report(report, report_dir=REPORT_DIR):
    try:
        version_history.attach_version_metadata(report)
        os.makedirs(report_dir, exist_ok=True)
        path = os.path.join(report_dir, f'futures_residual.{report.get("symbol")}.{_stamp()}.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write('\n')
        report['report_path'] = os.path.relpath(path, PROJECT_DIR)
    except Exception as exc:
        logging.warning('FUTURES RESIDUAL report write failed symbol=%s error=%s', report.get('symbol'), exc)
    return report


def _record(event, symbol, message, level='INFO', details=None):
    try:
        decision_timeline.record_event(
            event=event,
            message=message,
            level=level,
            category='PROTECTION',
            symbol=symbol,
            direction='SHORT',
            details=details or {},
        )
    except Exception:
        pass


def close_residual_reduce_only(client, state, pos, current, reason='managed_residual_auto_close', report_dir=REPORT_DIR):
    symbol = str(pos.get('symbol') or current.get('symbol') or '').upper()
    amount = _safe_float(current.get('position_amt'), 0.0) or 0.0
    quantity, filter_details = _round_quantity(client, symbol, abs(amount))
    if quantity is None:
        return {'ok': False, 'status': 'skipped', 'reason': filter_details.get('reason'), 'symbol': symbol, **filter_details}
    payload = {
        'symbol': symbol,
        'side': _close_side(amount),
        'type': 'MARKET',
        'quantity': str(quantity),
        'reduceOnly': 'true',
    }
    _record('futures_residual_close_attempt', symbol, f'{symbol} residual reduceOnly close attempt', details={'payload': payload, 'notional': current.get('notional')})
    order = client.create_futures_order(payload)
    after = refresh_position(client, symbol)
    after_amt = _safe_float(after.get('position_amt'), 0.0) or 0.0
    if abs(after_amt) <= POSITION_ZERO_TOLERANCE:
        _remove_state_position(state, symbol)
    result = {
        'ok': abs(after_amt) <= POSITION_ZERO_TOLERANCE,
        'status': 'closed' if abs(after_amt) <= POSITION_ZERO_TOLERANCE else 'not_fully_closed',
        'reason': reason,
        'symbol': symbol,
        'payload': payload,
        'before': current,
        'after': after,
        'order_id': order.get('orderId') if isinstance(order, dict) else None,
        'state_removed': abs(after_amt) <= POSITION_ZERO_TOLERANCE,
    }
    _write_report(result, report_dir=report_dir)
    _record('futures_residual_closed' if result['ok'] else 'futures_residual_close_incomplete', symbol, f'{symbol} residual close {result["status"]}', level='INFO' if result['ok'] else 'ERROR', details=result)
    return result


def recreate_short_protection(client, pos, current):
    symbol = str(pos.get('symbol') or '').upper()
    quantity = abs(_safe_float(current.get('position_amt'), _safe_float(pos.get('quantity'), 0.0)) or 0.0)
    if quantity <= 0:
        return {'ok': False, 'reason': 'no_quantity', 'symbol': symbol}
    tp = pos.get('tp')
    sl = pos.get('sl')
    result = {'ok': False, 'symbol': symbol, 'quantity': quantity, 'tp_order_id': '', 'sl_order_id': ''}
    if tp not in (None, ''):
        tp_order = client.create_futures_order({
            'symbol': symbol,
            'side': 'BUY',
            'type': 'LIMIT',
            'price': str(tp),
            'quantity': str(quantity),
            'reduceOnly': 'true',
            'timeInForce': 'GTC',
        })
        result['tp_order_id'] = str((tp_order or {}).get('orderId', ''))
        pos['tp_order_id'] = result['tp_order_id']
    if sl not in (None, '') and getattr(config, 'NATIVE_SL_ENABLED', True):
        sl_order = client.create_futures_order({
            'symbol': symbol,
            'side': 'BUY',
            'type': 'STOP_MARKET',
            'stopPrice': str(sl),
            'quantity': str(quantity),
            'reduceOnly': 'true',
        })
        result['sl_order_id'] = str((sl_order or {}).get('orderId', '') or (sl_order or {}).get('strategyId', '')) if sl_order else ''
        pos['sl_order_id'] = result['sl_order_id']
    orders = refresh_open_orders(client, symbol)
    result['open_orders_count'] = len(orders)
    result['ok'] = len(orders) > 0
    _record('futures_residual_protection_recreated' if result['ok'] else 'futures_residual_protection_failed', symbol, f'{symbol} protection recreate {"ok" if result["ok"] else "failed"}', level='INFO' if result['ok'] else 'CRITICAL', details=result)
    return result


def handle_after_partial_short(pos, state, client, out_fn=None, alert_fn=None, max_notional=None, report_dir=REPORT_DIR):
    symbol = str(pos.get('symbol') or '').upper()
    if not symbol:
        return {'ok': False, 'status': 'skipped', 'reason': 'missing_symbol'}
    max_notional = float(max_notional if max_notional is not None else getattr(config, 'FUTURES_RESIDUAL_MAX_NOTIONAL_USDT', 3.0))
    current = refresh_position(client, symbol)
    amount = _safe_float(current.get('position_amt'), 0.0) or 0.0
    if abs(amount) <= POSITION_ZERO_TOLERANCE:
        removed = _remove_state_position(state, symbol)
        result = {'ok': True, 'status': 'already_closed', 'symbol': symbol, 'state_removed_count': removed}
        _record('futures_residual_state_cleaned', symbol, f'{symbol} already closed; state cleaned', details=result)
        return result
    orders = refresh_open_orders(client, symbol)
    notional = abs(_safe_float(current.get('notional'), 0.0) or 0.0)
    if notional <= max_notional and getattr(config, 'FUTURES_RESIDUAL_CLOSE_ENABLED', True):
        cancelled = cancel_reduce_only_orders(client, symbol, orders)
        result = close_residual_reduce_only(client, state, pos, current, report_dir=report_dir)
        result['cancelled_reduce_only_orders'] = cancelled
        if out_fn:
            out_fn(f'Futures residual {symbol}: cierre reduceOnly {result.get("status")}')
        return result
    if not orders:
        recreated = recreate_short_protection(client, pos, current)
        if recreated.get('ok'):
            return {'ok': True, 'status': 'protection_recreated', 'symbol': symbol, 'recreated': recreated}
        state['futures_entries_blocked'] = True
        state['futures_entries_block_reason'] = 'Futures unprotected position present'
        msg = f'🚨 {symbol}: Futures sin protección y notional {notional:.2f} USDT. Nuevas entradas SHORT bloqueadas.'
        if alert_fn:
            alert_fn(msg)
        _record('futures_unprotected_block_entries', symbol, msg, level='CRITICAL', details={'notional': notional, 'threshold': max_notional, 'recreated': recreated})
        return {'ok': False, 'status': 'unprotected_large_position', 'symbol': symbol, 'notional': notional, 'threshold': max_notional, 'recreated': recreated}
    return {'ok': True, 'status': 'protected_or_not_residual', 'symbol': symbol, 'notional': notional, 'open_orders_count': len(orders)}


def has_unprotected_futures_risk(status=None):
    if not getattr(config, 'FUTURES_UNPROTECTED_BLOCK_NEW_ENTRIES', True):
        return False, None
    summary = futures_reconciliation.reconciliation_summary_from_status(status)
    try:
        count = int(summary.get('unprotected_count') or 0)
    except (TypeError, ValueError):
        count = 0
    if count > 0:
        return True, 'Futures unprotected position present'
    return False, None
