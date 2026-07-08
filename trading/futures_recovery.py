#!/usr/bin/env python3
"""Manual, explicit recovery for orphan/desynced Futures positions."""

import logging

import binance_client
import config
import decision_timeline
import futures_reconciliation
import futures_residuals
import utils


REQUIRED_CLASSIFICATIONS = {
    'unmanaged_futures_position',
    'orphan_futures_position',
    'unprotected_futures_position',
    'desynced_closed_but_open_on_exchange',
}
POSITION_ZERO_TOLERANCE = 1e-12


def _safe_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _status(status_file=None):
    return futures_reconciliation.load_status(status_file or futures_reconciliation.DEFAULT_STATUS_FILE)


def _entry(symbol, status_file=None):
    symbol = str(symbol or '').upper()
    positions = (_status(status_file).get('positions') or {})
    entry = positions.get(symbol)
    return entry if isinstance(entry, dict) else None


def _classification(entry):
    classes = entry.get('classification') if isinstance(entry, dict) else []
    return [str(item) for item in classes] if isinstance(classes, list) else []


def _is_recovery_candidate(entry):
    if not isinstance(entry, dict):
        return False
    if entry.get('managed_in_state'):
        return False
    return bool(REQUIRED_CLASSIFICATIONS.intersection(_classification(entry)))


def _normalise_position(raw):
    positions = futures_reconciliation.classify_positions([raw], state={'positions': []}, open_orders_by_symbol={})
    if positions:
        return next(iter(positions.values()))
    return None


def _current_position(client, symbol):
    rows = client.futures_position_risk({'symbol': symbol})
    if isinstance(rows, dict):
        rows = [rows]
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if str(row.get('symbol') or '').upper() != symbol:
            continue
        amount = _safe_float(row.get('positionAmt'), 0.0) or 0.0
        if abs(amount) > POSITION_ZERO_TOLERANCE:
            return _normalise_position(row)
    return None


def _close_side(position_amt):
    return 'BUY' if position_amt < 0 else 'SELL'


def _round_quantity(client, symbol, quantity):
    filters = client.get_futures_filters(symbol)
    filters = filters if isinstance(filters, dict) else {}
    step = _safe_float(filters.get('step_size'), 0.0) or 0.0
    min_qty = _safe_float(filters.get('min_qty'), 0.0) or 0.0
    rounded = utils.round_step(quantity, step) if step else quantity
    if rounded <= 0 or (min_qty > 0 and rounded < min_qty):
        return None, {
            'reason': 'unclosable_by_min_qty',
            'quantity': quantity,
            'rounded_quantity': rounded,
            'min_qty': min_qty,
            'step_size': step,
        }
    return rounded, {'min_qty': min_qty, 'step_size': step}


def _record(event, message, symbol=None, level='INFO', details=None):
    try:
        decision_timeline.record_event(
            event=event,
            message=message,
            level=level,
            category='GUARDIAN',
            symbol=symbol,
            direction='FUTURES',
            details=details or {},
        )
    except Exception:
        pass


def _binance_error_details(exc, payload=None):
    details = {}
    try:
        details = utils.extract_http_error_details(exc)
    except Exception:
        details = {}
    return {
        'error': str(exc),
        'status': details.get('status'),
        'code': details.get('code'),
        'msg': details.get('msg'),
        'raw_body': details.get('raw_body'),
        'payload': payload or {},
    }


def preview_recovery(status_file=None):
    status = _status(status_file)
    positions = status.get('positions') if isinstance(status.get('positions'), dict) else {}
    candidates = []
    for symbol, entry in positions.items():
        if not _is_recovery_candidate(entry):
            continue
        amount = _safe_float(entry.get('position_amt'), 0.0) or 0.0
        if abs(amount) <= POSITION_ZERO_TOLERANCE:
            continue
        quantity = abs(amount)
        side = _close_side(amount)
        candidate = {
            'symbol': symbol,
            'side': entry.get('side') or ('SHORT' if amount < 0 else 'LONG'),
            'position_amt': amount,
            'quantity': quantity,
            'notional': entry.get('notional'),
            'unrealized_pnl': entry.get('unrealized_pnl'),
            'classification': _classification(entry),
            'order': {
                'symbol': symbol,
                'side': side,
                'type': 'MARKET',
                'quantity': quantity,
                'reduceOnly': 'true',
            },
        }
        candidates.append(candidate)
    _record('futures_recovery_preview', f'Futures recovery preview: {len(candidates)} candidate(s)', details={'count': len(candidates)})
    return {'candidates': candidates, 'status_summary': status.get('summary') or {}}


def close_position(symbol, confirm=None, client=None, status_file=None):
    symbol = str(symbol or '').upper()
    client = client or binance_client.get_default_client()
    entry = _entry(symbol, status_file)
    _record('futures_recovery_close_requested', f'{symbol} recovery close requested', symbol=symbol, details={'confirm': confirm})

    if confirm != 'CONFIRM':
        result = {'ok': False, 'status': 'skipped', 'reason': 'missing_confirm', 'symbol': symbol}
        _record('futures_recovery_close_skipped', f'{symbol} recovery skipped: missing CONFIRM', symbol=symbol, level='WARNING', details=result)
        return result
    if not entry:
        result = {'ok': False, 'status': 'skipped', 'reason': 'symbol_not_reconciled', 'symbol': symbol}
        _record('futures_recovery_close_skipped', f'{symbol} recovery skipped: not reconciled', symbol=symbol, level='WARNING', details=result)
        return result
    if entry.get('managed_in_state'):
        result = {'ok': False, 'status': 'skipped', 'reason': 'managed_position', 'symbol': symbol}
        _record('futures_recovery_close_skipped', f'{symbol} recovery skipped: managed position', symbol=symbol, level='WARNING', details=result)
        return result
    if not REQUIRED_CLASSIFICATIONS.intersection(_classification(entry)):
        result = {'ok': False, 'status': 'skipped', 'reason': 'not_recovery_candidate', 'symbol': symbol, 'classification': _classification(entry)}
        _record('futures_recovery_close_skipped', f'{symbol} recovery skipped: not a recovery candidate', symbol=symbol, level='WARNING', details=result)
        return result

    before = _current_position(client, symbol)
    if not before:
        result = {'ok': True, 'status': 'already_closed', 'reason': 'no_open_position', 'symbol': symbol}
        _record('futures_recovery_close_success', f'{symbol} already closed on Binance', symbol=symbol, details=result)
        return result
    before_amt = _safe_float(before.get('position_amt'), 0.0) or 0.0
    quantity, filter_details = _round_quantity(client, symbol, abs(before_amt))
    if quantity is None:
        result = {'ok': False, 'status': 'skipped', 'symbol': symbol, **filter_details}
        _record('futures_recovery_close_skipped', f'{symbol} recovery skipped: min qty', symbol=symbol, level='WARNING', details=result)
        return result

    payload = {
        'symbol': symbol,
        'side': _close_side(before_amt),
        'type': 'MARKET',
        'quantity': str(quantity),
        'reduceOnly': 'true',
    }
    _record(
        'futures_recovery_close_attempt',
        f'{symbol} recovery close attempt',
        symbol=symbol,
        details={'before_position_amt': before_amt, 'payload': payload, 'classification': _classification(entry)},
    )
    try:
        order = client.create_futures_order(payload)
    except Exception as exc:
        details = _binance_error_details(exc, payload)
        logging.error('FUTURES RECOVERY CLOSE ERROR symbol=%s details=%s', symbol, details)
        _record('futures_recovery_close_error', f'{symbol} recovery close error', symbol=symbol, level='ERROR', details=details)
        return {'ok': False, 'status': 'error', 'symbol': symbol, **details}

    after = _current_position(client, symbol)
    after_amt = _safe_float((after or {}).get('position_amt'), 0.0) or 0.0
    success = abs(after_amt) <= POSITION_ZERO_TOLERANCE
    result = {
        'ok': success,
        'status': 'closed' if success else 'not_fully_closed',
        'symbol': symbol,
        'side': payload['side'],
        'quantity': quantity,
        'before_position_amt': before_amt,
        'after_position_amt': after_amt,
        'order_id': (order or {}).get('orderId') if isinstance(order, dict) else None,
    }
    _record(
        'futures_recovery_close_success' if success else 'futures_recovery_close_error',
        f'{symbol} recovery close {"success" if success else "not fully closed"}',
        symbol=symbol,
        level='INFO' if success else 'ERROR',
        details=result,
    )
    return result


def close_managed_residual(symbol, confirm=None, client=None, status_file=None,
                           max_notional=None, state=None, report_dir=None):
    symbol = str(symbol or '').upper()
    client = client or binance_client.get_default_client()
    entry = _entry(symbol, status_file)
    threshold = float(max_notional if max_notional is not None else getattr(config, 'FUTURES_RESIDUAL_MAX_NOTIONAL_USDT', 3.0))
    _record(
        'futures_managed_residual_close_requested',
        f'{symbol} managed residual close requested',
        symbol=symbol,
        details={'confirm': confirm, 'threshold': threshold},
    )

    if confirm != 'CONFIRM':
        result = {'ok': False, 'status': 'skipped', 'reason': 'missing_confirm', 'symbol': symbol}
        _record('futures_managed_residual_close_skipped', f'{symbol} managed residual skipped: missing CONFIRM', symbol=symbol, level='WARNING', details=result)
        return result
    if not entry:
        result = {'ok': False, 'status': 'skipped', 'reason': 'symbol_not_reconciled', 'symbol': symbol}
        _record('futures_managed_residual_close_skipped', f'{symbol} managed residual skipped: not reconciled', symbol=symbol, level='WARNING', details=result)
        return result
    if not entry.get('managed_in_state'):
        result = {'ok': False, 'status': 'skipped', 'reason': 'not_managed_position', 'symbol': symbol}
        _record('futures_managed_residual_close_skipped', f'{symbol} managed residual skipped: not managed in state', symbol=symbol, level='WARNING', details=result)
        return result
    classes = _classification(entry)
    open_orders_count = int(entry.get('open_orders_count') or 0)
    if 'unprotected_futures_position' not in classes and open_orders_count > 0:
        result = {
            'ok': False,
            'status': 'skipped',
            'reason': 'managed_position_has_protection',
            'symbol': symbol,
            'classification': classes,
            'open_orders_count': open_orders_count,
        }
        _record('futures_managed_residual_close_skipped', f'{symbol} managed residual skipped: protected', symbol=symbol, level='WARNING', details=result)
        return result
    notional = abs(_safe_float(entry.get('notional'), 0.0) or 0.0)
    if notional > threshold:
        result = {
            'ok': False,
            'status': 'skipped',
            'reason': 'notional_above_threshold',
            'symbol': symbol,
            'notional': notional,
            'threshold': threshold,
        }
        _record('futures_managed_residual_close_skipped', f'{symbol} managed residual skipped: notional above threshold', symbol=symbol, level='WARNING', details=result)
        return result

    current = futures_residuals.refresh_position(client, symbol)
    amount = _safe_float(current.get('position_amt'), 0.0) or 0.0
    if abs(amount) <= POSITION_ZERO_TOLERANCE:
        state = state if isinstance(state, dict) else utils.load_state()
        futures_residuals.remove_state_position(state, symbol)
        utils.save_state(state)
        result = {'ok': True, 'status': 'already_closed', 'reason': 'no_open_position', 'symbol': symbol}
        _record('futures_managed_residual_close_success', f'{symbol} already closed on Binance', symbol=symbol, details=result)
        return result

    state = state if isinstance(state, dict) else utils.load_state()
    result = futures_residuals.close_residual_reduce_only(
        client,
        state,
        {'symbol': symbol},
        current,
        reason='manual_managed_residual_close',
        report_dir=report_dir or futures_residuals.REPORT_DIR,
    )
    if result.get('ok'):
        utils.save_state(state)
    _record(
        'futures_managed_residual_close_success' if result.get('ok') else 'futures_managed_residual_close_error',
        f'{symbol} managed residual close {result.get("status")}',
        symbol=symbol,
        level='INFO' if result.get('ok') else 'ERROR',
        details=result,
    )
    return result


def format_preview_text(preview):
    candidates = preview.get('candidates') if isinstance(preview, dict) else []
    lines = ['🚨 Recovery Futures', '']
    if not candidates:
        lines.append('No hay posiciones candidatas para recovery manual.')
        return '\n'.join(lines)
    for item in candidates[:10]:
        lines.extend([
            f'{item["symbol"]} {item["side"]}',
            f'Cantidad: {item["quantity"]}',
            f'Notional: {item.get("notional")} USDT',
            f'uPnL: {item.get("unrealized_pnl")} USDT',
            'Orden propuesta:',
            f'{item["order"]["side"]} MARKET reduceOnly quantity={item["order"]["quantity"]}',
            f'Ejecutar: /futures_recovery_close {item["symbol"]} CONFIRM',
            '',
        ])
    return '\n'.join(lines).rstrip()


def format_close_result(result):
    if result.get('ok') and result.get('status') == 'closed':
        return (
            '✅ Futures recovery ejecutado\n\n'
            f'{result.get("symbol")}\n'
            f'Orden: {result.get("side")} MARKET reduceOnly\n'
            f'Cantidad: {result.get("quantity")}\n'
            f'Antes: {result.get("before_position_amt")}\n'
            f'Despues: {result.get("after_position_amt")}\n'
            f'Order ID: {result.get("order_id") or "N/D"}'
        )
    if result.get('ok') and result.get('status') == 'already_closed':
        return f'✅ {result.get("symbol")} ya estaba cerrada en Binance.'
    return (
        '⚠️ Futures recovery no ejecutado\n\n'
        f'Simbolo: {result.get("symbol")}\n'
        f'Motivo: {result.get("reason") or result.get("status")}\n'
        f'code={result.get("code")}\n'
        f'msg={result.get("msg") or result.get("error")}'
    )
