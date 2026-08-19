#!/usr/bin/env python3
"""Conservative preventive close lifecycle for managed SHORT Futures positions."""

from decimal import Decimal, InvalidOperation

import config
import decision_timeline
import futures_residuals


ZERO_TOLERANCE = futures_residuals.POSITION_ZERO_TOLERANCE


def _decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _error_text(exc):
    return f'{type(exc).__name__}: {exc}'


def _record(status, pos, details=None, level='INFO'):
    symbol = str((pos or {}).get('symbol') or '').upper()
    try:
        decision_timeline.record_event(
            event='preventive_futures_close',
            message=f'{symbol} preventive SHORT close: {status}',
            level=level,
            category='PROTECTION',
            symbol=symbol,
            direction='SHORT',
            related_trade_id=(pos or {}).get('id'),
            details={'status': status, **(details or {})},
        )
    except Exception:
        pass


def _result(status, pos, **details):
    result = {
        'status': status,
        'symbol': str((pos or {}).get('symbol') or '').upper(),
        'confirmed_close': status == 'CONFIRMED_PREVENTIVE_CLOSE',
        **details,
    }
    level = 'INFO' if status in {'CONFIRMED_PREVENTIVE_CLOSE', 'ALREADY_FLAT'} else 'WARNING'
    _record(status, pos, details=result, level=level)
    return result


def _fill_evidence(order, expected_quantity):
    order = order if isinstance(order, dict) else {}
    order_id = order.get('orderId')
    executed = _decimal(order.get('executedQty'))
    avg_price = _decimal(order.get('avgPrice'))
    if (avg_price is None or avg_price <= 0) and executed and executed > 0:
        quote = _decimal(order.get('cumQuote') or order.get('cumQuoteQty') or order.get('cummulativeQuoteQty'))
        if quote is not None and quote > 0:
            avg_price = quote / executed
    if avg_price is None or avg_price <= 0:
        fills = order.get('fills') if isinstance(order.get('fills'), list) else []
        fill_qty = sum((_decimal(item.get('qty')) or Decimal('0')) for item in fills if isinstance(item, dict))
        fill_quote = sum(
            (_decimal(item.get('qty')) or Decimal('0')) * (_decimal(item.get('price')) or Decimal('0'))
            for item in fills if isinstance(item, dict)
        )
        if fill_qty > 0 and fill_quote > 0:
            executed = executed if executed and executed > 0 else fill_qty
            avg_price = fill_quote / fill_qty
    expected = _decimal(expected_quantity) or Decimal('0')
    tolerance = max(Decimal(str(ZERO_TOLERANCE)), abs(expected) * Decimal('0.000000001'))
    attributable = bool(
        order_id not in (None, '')
        and str(order.get('status') or '').upper() == 'FILLED'
        and executed is not None and executed > 0
        and executed + tolerance >= expected
        and avg_price is not None and avg_price > 0
    )
    return {
        'attributable': attributable,
        'order_id': order_id,
        'order_status': str(order.get('status') or '').upper() or None,
        'executed_quantity': float(executed) if executed is not None else None,
        'fill_price': float(avg_price) if avg_price is not None else None,
    }


def _cleanup_protection(client, pos):
    symbol = str(pos.get('symbol') or '').upper()
    order_ids = []
    errors = []
    try:
        for order in futures_residuals.refresh_open_orders(client, symbol):
            if futures_residuals._is_reduce_only(order) and order.get('orderId') not in (None, ''):
                order_ids.append(order['orderId'])
    except Exception as exc:
        errors.append({'stage': 'list_open_orders', 'error': _error_text(exc)})
    for key in ('tp_order_id', 'sl_order_id'):
        if pos.get(key) not in (None, ''):
            order_ids.append(pos[key])
    cancelled = []
    seen = set()
    for order_id in order_ids:
        marker = str(order_id)
        if marker in seen:
            continue
        seen.add(marker)
        try:
            client.cancel_futures_order({'symbol': symbol, 'orderId': order_id})
            cancelled.append(order_id)
        except Exception as exc:
            errors.append({'stage': 'cancel_protection', 'order_id': order_id, 'error': _error_text(exc)})
    return {'cancelled_order_ids': cancelled, 'cleanup_errors': errors}


def attempt_preventive_short_close(client, pos):
    """Attempt one real reduce-only close and classify the exchange-confirmed outcome.

    This function never mutates local position state or writes trade/PnL records.  A
    caller may finalize a close only when ``confirmed_close`` is true.
    """
    symbol = str((pos or {}).get('symbol') or '').upper()
    if str((pos or {}).get('direction') or '').lower() != 'short':
        return _result('NOT_A_SHORT', pos)
    try:
        before = futures_residuals.refresh_position(client, symbol)
    except Exception as exc:
        return _result('PREFLIGHT_POSITION_UNKNOWN', pos, error=_error_text(exc))
    before_amount = float(before.get('position_amt') or 0.0)
    if abs(before_amount) <= ZERO_TOLERANCE:
        return _result('ALREADY_FLAT', pos, before_position_amt=before_amount)
    if before_amount > 0:
        return _result('DIRECTION_MISMATCH', pos, before_position_amt=before_amount)

    try:
        quantity, quantity_details = futures_residuals._round_quantity(client, symbol, abs(before_amount))
    except Exception as exc:
        return _result(
            'CLOSE_QUANTITY_UNKNOWN', pos,
            before_position_amt=before_amount,
            error=_error_text(exc),
        )
    if quantity is None:
        return _result(
            'INVALID_CLOSE_QUANTITY', pos,
            before_position_amt=before_amount,
            quantity_details=quantity_details,
        )
    payload = {
        'symbol': symbol,
        'side': 'BUY',
        'type': 'MARKET',
        'quantity': str(quantity),
        'reduceOnly': 'true',
    }
    order = None
    post_error = None
    try:
        order = client.create_futures_order(payload)
    except Exception as exc:
        post_error = _error_text(exc)

    try:
        after = futures_residuals.refresh_position(client, symbol)
    except Exception as exc:
        return _result(
            'POST_POSITION_UNKNOWN', pos,
            before_position_amt=before_amount,
            requested_quantity=quantity,
            order_id=order.get('orderId') if isinstance(order, dict) else None,
            post_error=post_error,
            position_error=_error_text(exc),
        )
    after_amount = float(after.get('position_amt') or 0.0)

    evidence_order = dict(order) if isinstance(order, dict) else {}
    order_id = evidence_order.get('orderId')
    order_lookup_error = None
    if order_id not in (None, ''):
        try:
            looked_up = client.get_futures_order({'symbol': symbol, 'orderId': order_id})
            if isinstance(looked_up, dict):
                evidence_order.update(looked_up)
        except Exception as exc:
            order_lookup_error = _error_text(exc)
    evidence = _fill_evidence(evidence_order, abs(before_amount))

    if abs(after_amount) <= ZERO_TOLERANCE:
        cleanup = _cleanup_protection(client, pos)
        common = {
            'before_position_amt': before_amount,
            'after_position_amt': after_amount,
            'requested_quantity': quantity,
            'post_error': post_error,
            'order_lookup_error': order_lookup_error,
            **evidence,
            **cleanup,
        }
        if evidence['attributable'] and post_error is None:
            entry = float(pos.get('entry_price') or 0.0)
            executed = evidence['executed_quantity']
            fill_price = evidence['fill_price']
            common['pnl'] = (entry - fill_price) * executed * (1 - config.FUTURES_FEE_RATE * 2)
            return _result('CONFIRMED_PREVENTIVE_CLOSE', pos, **common)
        return _result('FLAT_UNATTRIBUTED', pos, **common)

    common = {
        'before_position_amt': before_amount,
        'after_position_amt': after_amount,
        'requested_quantity': quantity,
        'post_error': post_error,
        'order_lookup_error': order_lookup_error,
        **evidence,
    }
    if after_amount > 0:
        return _result('POST_DIRECTION_MISMATCH', pos, **common)
    if abs(after_amount) + ZERO_TOLERANCE < abs(before_amount):
        return _result('RESIDUAL_POSITION', pos, remaining_quantity=abs(after_amount), **common)
    return _result('POSITION_STILL_OPEN', pos, remaining_quantity=abs(after_amount), **common)
