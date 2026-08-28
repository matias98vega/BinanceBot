#!/usr/bin/env python3
"""Fail-closed preventive close lifecycle for managed LONG Spot positions."""

import hashlib
from decimal import Decimal, InvalidOperation, ROUND_DOWN

import config
import decision_timeline
import longs
import residuals


FINAL_CLOSE_STATUSES = {
    'CONFIRMED_PREVENTIVE_CLOSE',
    'CONFIRMED_PREVENTIVE_CLOSE_WITH_DUST',
}


def _decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _error_text(exc):
    return f'{type(exc).__name__}: {exc}'


def _json_safe(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _asset_from_symbol(symbol):
    text = str(symbol or '').upper()
    return text[:-4] if text.endswith('USDT') else ''


def _format_decimal(value):
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _record(status, pos, details=None, level='INFO'):
    symbol = str((pos or {}).get('symbol') or '').upper()
    try:
        decision_timeline.record_event(
            event='preventive_spot_close',
            message=f'{symbol} preventive LONG Spot close: {status}',
            level=level,
            category='PROTECTION',
            symbol=symbol,
            direction='LONG',
            related_trade_id=(pos or {}).get('trade_id') or (pos or {}).get('id'),
            details={'status': status, **_json_safe(details or {})},
        )
    except Exception:
        pass


def _result(status, pos, **details):
    result = {
        'status': status,
        'symbol': str((pos or {}).get('symbol') or '').upper(),
        'confirmed_close': status in FINAL_CLOSE_STATUSES,
        **details,
    }
    if result['confirmed_close'] or status in {'ALREADY_FLAT_UNATTRIBUTED', 'NON_OPERABLE_PROTECTED'}:
        level = 'INFO'
    elif status.endswith('_PROTECTED'):
        level = 'WARNING'
    else:
        level = 'CRITICAL'
    _record(status, pos, details=result, level=level)
    return result


def _balance(account, asset):
    rows = account.get('balances') if isinstance(account, dict) else None
    for row in rows or []:
        if str((row or {}).get('asset') or '').upper() == asset:
            free = _decimal(row.get('free'))
            locked = _decimal(row.get('locked'))
            if free is None or locked is None or free < 0 or locked < 0:
                break
            return {'free': free, 'locked': locked, 'total': free + locked}
    raise ValueError(f'fresh Spot balance unavailable for {asset}')


def _filter_values(filters, market=False):
    filters = filters if isinstance(filters, dict) else {}
    prefix = 'market_' if market else ''
    step = _decimal(filters.get(f'{prefix}step_size'))
    min_qty = _decimal(filters.get(f'{prefix}min_qty'))
    max_qty = _decimal(filters.get(f'{prefix}max_qty'))
    if step is None or step <= 0:
        step = _decimal(filters.get('step_size'))
    if min_qty is None or min_qty <= 0:
        min_qty = _decimal(filters.get('min_qty'))
    if max_qty is None or max_qty <= 0:
        max_qty = _decimal(filters.get('max_qty'))
    min_notional = _decimal(filters.get('min_notional'))
    if step is None or step <= 0 or min_qty is None or min_qty < 0 or min_notional is None or min_notional < 0:
        raise ValueError('incomplete Spot quantity filters')
    return step, min_qty, max_qty, min_notional


def _normalize_quantity(quantity, filters, price, market=False):
    qty = _decimal(quantity)
    px = _decimal(price)
    if qty is None or qty <= 0 or px is None or px <= 0:
        return Decimal('0'), {'reason': 'invalid_quantity_or_price'}
    step, min_qty, max_qty, min_notional = _filter_values(filters, market=market)
    normalized = (qty / step).to_integral_value(rounding=ROUND_DOWN) * step
    if max_qty is not None and max_qty > 0:
        normalized = min(normalized, max_qty)
    reason = None
    if normalized <= 0 or normalized < min_qty:
        reason = 'below_min_qty'
    elif normalized * px < min_notional:
        reason = 'below_min_notional'
    return (Decimal('0') if reason else normalized), {
        'reason': reason,
        'step': step,
        'min_qty': min_qty,
        'max_qty': max_qty,
        'min_notional': min_notional,
        'rounded_quantity': normalized,
        'notional': normalized * px,
    }


def _client_order_id(pos, symbol):
    trade_id = str((pos or {}).get('trade_id') or (pos or {}).get('id') or '')
    digest = hashlib.sha256(f'{trade_id}|{symbol}|preventive-long-spot-v1'.encode('utf-8')).hexdigest()[:24]
    return f'psl_{digest}'


def _open_orders(client, symbol):
    rows = client.spot_signed('GET', '/api/v3/openOrders', {'symbol': symbol})
    if not isinstance(rows, list):
        raise ValueError('Spot open orders response is not a list')
    return rows


def _validate_oco(client, pos, symbol, expected_quantity, step, open_orders):
    canonical_id = pos.get('oco_order_list_id')
    legacy_id = pos.get('oco_id')
    if canonical_id in (None, ''):
        if legacy_id not in (None, ''):
            return None, {'status': 'LEGACY_OCO_IDENTIFIER'}
        if open_orders:
            return None, {'status': 'UNTRACKED_SPOT_OPEN_ORDERS'}
        return None, None
    try:
        order_list = client.get_order_list({'orderListId': int(canonical_id)})
    except Exception as exc:
        return None, {'status': 'OCO_LOOKUP_FAILED', 'error': _error_text(exc)}
    if not isinstance(order_list, dict) or str(order_list.get('orderListId')) != str(canonical_id):
        return None, {'status': 'OCO_ID_MISMATCH'}
    if str(order_list.get('listOrderStatus') or '').upper() != 'EXECUTING':
        return None, {'status': 'OCO_NOT_EXECUTING', 'list_status': order_list.get('listOrderStatus')}

    listed_ids = {
        str(item.get('orderId')) for item in order_list.get('orders') or []
        if isinstance(item, dict) and item.get('orderId') not in (None, '')
    }
    stored_ids = {str(value) for value in pos.get('oco_order_ids') or [] if value not in (None, '')}
    if not listed_ids or (stored_ids and listed_ids != stored_ids):
        return None, {'status': 'OCO_LEG_IDS_MISMATCH'}
    relevant = [
        order for order in open_orders
        if str((order or {}).get('orderListId')) == str(canonical_id)
    ]
    if len(relevant) != len(listed_ids):
        return None, {'status': 'OCO_OPEN_LEGS_MISMATCH'}
    quantities = []
    for order in relevant:
        if str(order.get('symbol') or '').upper() != symbol or str(order.get('side') or '').upper() != 'SELL':
            return None, {'status': 'OCO_CONTRACT_MISMATCH'}
        if str(order.get('orderId')) not in listed_ids:
            return None, {'status': 'OCO_LEG_IDS_MISMATCH'}
        quantity = _decimal(order.get('origQty'))
        executed = _decimal(order.get('executedQty')) or Decimal('0')
        if quantity is None or quantity <= 0 or executed < 0 or executed > quantity:
            return None, {'status': 'OCO_QUANTITY_INVALID'}
        quantities.append(quantity - executed)
    protected_quantity = min(quantities)
    if abs(protected_quantity - expected_quantity) > step:
        return None, {
            'status': 'OCO_QUANTITY_MISMATCH',
            'protected_quantity': float(protected_quantity),
            'expected_quantity': float(expected_quantity),
        }
    return {
        'order_list_id': int(canonical_id),
        'order_ids': sorted(listed_ids),
        'protected_quantity': protected_quantity,
    }, None


def _fill_evidence(order, expected_client_order_id):
    order = order if isinstance(order, dict) else {}
    executed = _decimal(order.get('executedQty'))
    quote = _decimal(order.get('cummulativeQuoteQty') or order.get('cumQuoteQty') or order.get('cumQuote'))
    fills = order.get('fills') if isinstance(order.get('fills'), list) else []
    fill_qty = sum((_decimal(item.get('qty')) or Decimal('0')) for item in fills if isinstance(item, dict))
    fill_quote = sum(
        (_decimal(item.get('qty')) or Decimal('0')) * (_decimal(item.get('price')) or Decimal('0'))
        for item in fills if isinstance(item, dict)
    )
    if executed is None or executed <= 0:
        executed = fill_qty
    if quote is None or quote <= 0:
        quote = fill_quote
    fill_price = quote / executed if executed and executed > 0 and quote and quote > 0 else None
    client_order_id = order.get('clientOrderId') or order.get('origClientOrderId')
    attributable = bool(
        (order.get('orderId') not in (None, '') or client_order_id == expected_client_order_id)
        and str(order.get('symbol') or '').upper()
        and str(order.get('side') or '').upper() == 'SELL'
        and str(order.get('type') or '').upper() == 'MARKET'
        and str(order.get('status') or '').upper() in {'FILLED', 'PARTIALLY_FILLED'}
        and executed is not None and executed > 0
        and fill_price is not None and fill_price > 0
    )
    commissions = [
        {
            'asset': item.get('commissionAsset'),
            'amount': float(_decimal(item.get('commission')) or Decimal('0')),
        }
        for item in fills if isinstance(item, dict) and item.get('commission') not in (None, '')
    ]
    return {
        'attributable': attributable,
        'order_id': order.get('orderId'),
        'client_order_id': client_order_id,
        'order_status': str(order.get('status') or '').upper() or None,
        'executed_quantity': executed,
        'quote_quantity': quote,
        'fill_price': fill_price,
        'commissions': commissions,
    }


def _mark_recovery(pos, status, quantity, warning):
    pos['quantity'] = float(max(quantity, Decimal('0')))
    pos['oco_order_list_id'] = ''
    pos['oco_order_ids'] = []
    pos['recovery_pending'] = True
    pos['preventive_close_status'] = status
    pos['protection_warning'] = warning


def _restore_protection(client, pos, symbol, quantity, price, filters, residual_handler):
    quantity, quantity_details = _normalize_quantity(quantity, filters, price, market=False)
    if quantity <= 0:
        try:
            residual_handler(
                symbol,
                _asset_from_symbol(symbol),
                float(quantity_details.get('rounded_quantity') or 0),
                float(price),
                filters,
                reason=quantity_details.get('reason') or 'below_min_notional',
            )
        except Exception:
            pass
        return {'restored': False, 'unprotectable': True, 'quantity_details': quantity_details}
    tp = _decimal(pos.get('tp'))
    sl = _decimal(pos.get('sl'))
    tick = _decimal(filters.get('tick_size'))
    if tp is None or tp <= 0 or sl is None or sl <= 0 or tick is None or tick <= 0:
        return {'restored': False, 'error': 'missing canonical TP/SL/tick evidence'}
    params = longs._build_oco_params(symbol, float(quantity), float(tp), float(sl), float(tick))
    payload_check = residuals.validate_spot_oco_payload_notional(params, filters)
    if not payload_check.get('should_send_oco'):
        try:
            residual_handler(
                symbol,
                _asset_from_symbol(symbol),
                float(quantity),
                float(price),
                filters,
                reason=payload_check.get('reason') or 'oco_payload_below_min_notional',
                oco_payload=params,
            )
        except Exception:
            pass
        return {'restored': False, 'unprotectable': True, 'payload_check': payload_check}
    try:
        response = client.create_oco(params)
    except Exception as exc:
        return {'restored': False, 'error': _error_text(exc)}
    list_id = response.get('orderListId') if isinstance(response, dict) else None
    order_ids = [
        str(item.get('orderId')) for item in (response.get('orders') or [])
        if isinstance(item, dict) and item.get('orderId') not in (None, '')
    ] if isinstance(response, dict) else []
    if list_id in (None, '') or not order_ids:
        return {'restored': False, 'error': 'incomplete OCO creation response'}
    pos['quantity'] = float(quantity)
    pos['oco_order_list_id'] = str(list_id)
    pos['oco_order_ids'] = order_ids
    pos['recovery_pending'] = False
    pos.pop('preventive_close_status', None)
    pos.pop('protection_warning', None)
    return {'restored': True, 'order_list_id': list_id, 'order_ids': order_ids, 'quantity': float(quantity)}


def attempt_preventive_long_spot_close(client, pos, residual_handler=None):
    """Attempt one idempotent Spot SELL and classify exchange-confirmed evidence.

    The helper may update protection/recovery fields on ``pos`` after an OCO was
    cancelled. It never writes trade analytics, PnL, state, or history and never
    asks the caller to delete local state unless ``confirmed_close`` is true.
    """
    residual_handler = residual_handler or residuals.handle_unprotectable_spot_residual
    direction = str((pos or {}).get('direction') or '').lower()
    symbol = str((pos or {}).get('symbol') or '').upper()
    trade_id = (pos or {}).get('trade_id') or (pos or {}).get('id')
    local_quantity = _decimal((pos or {}).get('quantity'))
    entry_price = _decimal((pos or {}).get('entry_price') or (pos or {}).get('entry'))
    asset = _asset_from_symbol(symbol)
    if direction != 'long' or not trade_id or not symbol.endswith('USDT') or not asset:
        return _result('INVALID_MANAGED_SPOT_LONG', pos)
    if local_quantity is None or local_quantity <= 0 or entry_price is None or entry_price <= 0:
        return _result('INVALID_MANAGED_SPOT_LONG', pos)
    if pos.get('oco_order_list_id') in (None, '') and pos.get('oco_id') not in (None, ''):
        return _result('LEGACY_OCO_IDENTIFIER', pos)

    try:
        filters = client.get_spot_filters(symbol)
        if str(filters.get('status') or '').upper() != 'TRADING':
            return _result('SPOT_SYMBOL_NOT_TRADING', pos, market_status=filters.get('status'))
        price = _decimal(client.get_spot_price(symbol))
        before_balance = _balance(client.get_spot_account(), asset)
        open_orders = _open_orders(client, symbol)
        market_step, _, _, _ = _filter_values(filters, market=True)
        oco_step, _, _, _ = _filter_values(filters, market=False)
    except Exception as exc:
        return _result('PREFLIGHT_EXCHANGE_UNKNOWN', pos, error=_error_text(exc))

    managed_raw = min(local_quantity, before_balance['total'])
    managed_quantity, quantity_details = _normalize_quantity(managed_raw, filters, price, market=True)
    excess = max(before_balance['total'] - managed_raw, Decimal('0'))
    excess_operable, _ = _normalize_quantity(excess, filters, price, market=True)
    if excess_operable > 0:
        return _result(
            'EXCHANGE_BALANCE_EXCEEDS_LOCAL', pos,
            local_quantity=float(local_quantity),
            exchange_quantity=float(before_balance['total']),
            excess_quantity=float(excess),
        )

    oco, oco_error = _validate_oco(client, pos, symbol, managed_quantity, oco_step, open_orders)
    if oco_error:
        return _result(oco_error.pop('status'), pos, **oco_error)
    if managed_quantity <= 0:
        if oco:
            return _result(
                'NON_OPERABLE_PROTECTED', pos,
                exchange_quantity=float(before_balance['total']),
                quantity_details=quantity_details,
            )
        try:
            residual_handler(
                symbol, asset, float(before_balance['total']), float(price), filters,
                reason=quantity_details.get('reason') or 'below_min_notional',
            )
        except Exception:
            pass
        return _result(
            'ALREADY_FLAT_UNATTRIBUTED', pos,
            exchange_quantity=float(before_balance['total']),
            quantity_details=quantity_details,
        )
    if not oco and before_balance['locked'] > 0:
        return _result('LOCKED_BALANCE_WITHOUT_CANONICAL_OCO', pos, locked=float(before_balance['locked']))

    protection_cancelled = False
    if oco:
        try:
            client.cancel_order_list({'symbol': symbol, 'orderListId': oco['order_list_id']})
            protection_cancelled = True
            pos['oco_order_list_id'] = ''
            pos['oco_order_ids'] = []
        except Exception as exc:
            return _result('CANCEL_OCO_FAILED', pos, error=_error_text(exc))
    try:
        released_balance = _balance(client.get_spot_account(), asset)
    except Exception as exc:
        if protection_cancelled:
            _mark_recovery(pos, 'BALANCE_AFTER_CANCEL_UNKNOWN', managed_quantity, _error_text(exc))
        return _result('BALANCE_AFTER_CANCEL_UNKNOWN', pos, error=_error_text(exc))
    if released_balance['free'] + market_step < managed_quantity:
        restore = _restore_protection(
            client, pos, symbol, min(managed_quantity, released_balance['total']), price, filters, residual_handler,
        ) if protection_cancelled else {'restored': False}
        status = 'BALANCE_NOT_RELEASED_PROTECTED' if restore.get('restored') else 'BALANCE_NOT_RELEASED'
        if not restore.get('restored'):
            _mark_recovery(pos, status, min(managed_quantity, released_balance['total']), 'balance not released after OCO cancel')
        return _result(status, pos, restore=restore)

    client_order_id = _client_order_id(pos, symbol)
    payload = {
        'symbol': symbol,
        'side': 'SELL',
        'type': 'MARKET',
        'quantity': _format_decimal(managed_quantity),
        'newClientOrderId': client_order_id,
    }
    order = None
    post_error = None
    try:
        order = client.create_spot_order(payload)
    except Exception as exc:
        post_error = _error_text(exc)

    evidence_order = dict(order) if isinstance(order, dict) else {}
    lookup_error = None
    lookup = {'symbol': symbol}
    if evidence_order.get('orderId') not in (None, ''):
        lookup['orderId'] = evidence_order['orderId']
    else:
        lookup['origClientOrderId'] = client_order_id
    try:
        looked_up = client.get_spot_order(lookup)
        if isinstance(looked_up, dict):
            evidence_order.update(looked_up)
    except Exception as exc:
        lookup_error = _error_text(exc)
    evidence = _fill_evidence(evidence_order, client_order_id)

    try:
        after_balance = _balance(client.get_spot_account(), asset)
    except Exception as exc:
        _mark_recovery(pos, 'POST_BALANCE_UNKNOWN', managed_quantity, _error_text(exc))
        return _result(
            'POST_BALANCE_UNKNOWN', pos,
            requested_quantity=float(managed_quantity),
            post_error=post_error,
            lookup_error=lookup_error,
            order_id=evidence.get('order_id'),
        )

    balance_delta = max(before_balance['total'] - after_balance['total'], Decimal('0'))
    executed = evidence.get('executed_quantity') or Decimal('0')
    confirmed_executed = min(executed, balance_delta) if evidence.get('attributable') else Decimal('0')
    remaining_managed = max(managed_quantity - confirmed_executed, Decimal('0'))
    remaining_operable, remaining_details = _normalize_quantity(remaining_managed, filters, price, market=True)

    if not evidence.get('attributable'):
        if balance_delta <= market_step:
            restore = _restore_protection(
                client, pos, symbol, min(managed_quantity, after_balance['total']), price, filters, residual_handler,
            )
            status = 'MARKET_FAILED_PROTECTED' if restore.get('restored') else 'MARKET_FAILED_UNPROTECTED'
            if not restore.get('restored'):
                _mark_recovery(pos, status, min(managed_quantity, after_balance['total']), post_error or lookup_error or 'missing fill evidence')
            return _result(status, pos, post_error=post_error, lookup_error=lookup_error, restore=restore)
        _mark_recovery(pos, 'AMBIGUOUS_CLOSE', min(managed_quantity, after_balance['total']), 'balance changed without attributable fill')
        return _result(
            'AMBIGUOUS_CLOSE', pos,
            post_error=post_error,
            lookup_error=lookup_error,
            balance_delta=float(balance_delta),
        )

    if abs(balance_delta - executed) > market_step:
        _mark_recovery(pos, 'AMBIGUOUS_BALANCE_DELTA', min(managed_quantity, after_balance['total']), 'fill and balance delta disagree')
        return _result(
            'AMBIGUOUS_BALANCE_DELTA', pos,
            executed_quantity=float(executed),
            balance_delta=float(balance_delta),
        )

    if remaining_operable > 0:
        pos['quantity'] = float(remaining_operable)
        restore = _restore_protection(client, pos, symbol, remaining_operable, price, filters, residual_handler)
        status = 'PARTIAL_RESIDUAL_PROTECTED' if restore.get('restored') else 'PARTIAL_RESIDUAL_UNPROTECTED'
        if not restore.get('restored'):
            _mark_recovery(pos, status, remaining_operable, 'partial preventive SELL left an unprotected residual')
        return _result(
            status, pos,
            requested_quantity=float(managed_quantity),
            executed_quantity=float(executed),
            remaining_quantity=float(remaining_operable),
            restore=restore,
        )

    residual_recorded = False
    if remaining_managed > 0:
        try:
            residual_recorded = bool(residual_handler(
                symbol,
                asset,
                float(remaining_managed),
                float(price),
                filters,
                reason=remaining_details.get('reason') or 'below_min_notional',
            ))
        except Exception:
            residual_recorded = False
        if not residual_recorded:
            _mark_recovery(
                pos,
                'DUST_RECORD_FAILED',
                remaining_managed,
                'non-operable residual could not be persisted',
            )
            return _result(
                'DUST_RECORD_FAILED',
                pos,
                requested_quantity=float(managed_quantity),
                executed_quantity=float(executed),
                remaining_quantity=float(remaining_managed),
            )
    fill_price = evidence['fill_price']
    executed_float = float(executed)
    pnl = (float(fill_price) - float(entry_price)) * executed_float * (1 - config.BNB_FEE_RATE * 2)
    status = 'CONFIRMED_PREVENTIVE_CLOSE_WITH_DUST' if remaining_managed > 0 else 'CONFIRMED_PREVENTIVE_CLOSE'
    return _result(
        status, pos,
        requested_quantity=float(managed_quantity),
        executed_quantity=executed_float,
        fill_price=float(fill_price),
        quote_quantity=float(evidence['quote_quantity']),
        commissions=evidence['commissions'],
        pnl=pnl,
        remaining_quantity=float(remaining_managed),
        residual_recorded=residual_recorded,
        client_order_id=client_order_id,
        order_id=evidence['order_id'],
        post_error=post_error,
        lookup_error=lookup_error,
    )
