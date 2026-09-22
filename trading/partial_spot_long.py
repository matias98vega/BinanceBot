#!/usr/bin/env python3
"""Fail-closed exchange flow for a managed Spot LONG partial close."""

import hashlib
import time
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError

import config
import residuals
import utils
from spot_recovery_lock import is_spot_long_recovery_pending
from quantity_integrity import (
    compute_partial_and_remaining,
    decimal_value,
    format_decimal_quantity,
    normalize_quantity_to_step,
    remaining_after_execution,
)


def _decimal(value):
    try:
        result = decimal_value(value)
        return result if result.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _result(status, **details):
    return {'status': status, 'confirmed_execution': False, **details}


def _asset(symbol):
    return symbol[:-4] if symbol.endswith('USDT') else ''


def _balance(account, asset):
    rows = account.get('balances') if isinstance(account, dict) else None
    for row in rows or []:
        if str((row or {}).get('asset') or '').upper() == asset:
            free = _decimal(row.get('free'))
            locked = _decimal(row.get('locked') or 0)
            if free is not None and locked is not None and free >= 0 and locked >= 0:
                return {'free': free, 'locked': locked, 'total': free + locked}
    raise ValueError(f'fresh Spot balance unavailable for {asset}')


def _filters(filters, market=False):
    filters = filters if isinstance(filters, dict) else {}
    prefix = 'market_' if market else ''
    step = _decimal(filters.get(f'{prefix}step_size')) or _decimal(filters.get('step_size'))
    min_qty = _decimal(filters.get(f'{prefix}min_qty'))
    if min_qty is None or min_qty <= 0:
        min_qty = _decimal(filters.get('min_qty'))
    max_qty = _decimal(filters.get(f'{prefix}max_qty'))
    if max_qty is None or max_qty <= 0:
        max_qty = _decimal(filters.get('max_qty'))
    min_notional = _decimal(filters.get('min_notional'))
    if step is None or step <= 0 or min_qty is None or min_qty < 0:
        raise ValueError('incomplete Spot quantity filters')
    if min_notional is None or min_notional < 0:
        raise ValueError('incomplete Spot notional filter')
    return step, min_qty, max_qty, min_notional


def _normalize(quantity, filters, price, market=False):
    step, min_qty, max_qty, min_notional = _filters(filters, market=market)
    qty = normalize_quantity_to_step(quantity, step)
    px = _decimal(price)
    if max_qty is not None and max_qty > 0:
        qty = min(qty, normalize_quantity_to_step(max_qty, step))
    reason = None
    if qty <= 0 or qty < min_qty:
        reason = 'below_min_qty'
    elif px is None or px <= 0:
        reason = 'invalid_price'
    elif qty * px < min_notional:
        reason = 'below_min_notional'
    return (Decimal('0') if reason else qty), {
        'reason': reason,
        'step': step,
        'min_qty': min_qty,
        'max_qty': max_qty,
        'min_notional': min_notional,
        'rounded_quantity': qty,
    }


def _open_orders(client, symbol):
    if hasattr(client, 'spot_open_orders'):
        rows = client.spot_open_orders({'symbol': symbol})
    else:
        rows = client.spot_signed('GET', '/api/v3/openOrders', {'symbol': symbol})
    if not isinstance(rows, list):
        raise ValueError('Spot open orders response is not a list')
    return rows


def _get_order_list(client, params):
    if hasattr(client, 'get_order_list'):
        return client.get_order_list(params)
    return client.spot_signed('GET', '/api/v3/orderList', params)


def _cancel_order_list(client, params):
    if hasattr(client, 'cancel_order_list'):
        return client.cancel_order_list(params)
    return client.spot_signed('DELETE', '/api/v3/orderList', params)


def _create_order(client, params):
    if hasattr(client, 'create_spot_order'):
        return client.create_spot_order(params)
    return client.spot_signed('POST', '/api/v3/order', params)


def _get_order(client, params):
    if hasattr(client, 'get_spot_order'):
        return client.get_spot_order(params)
    return client.spot_signed('GET', '/api/v3/order', params)


def _create_oco(client, params):
    if hasattr(client, 'create_oco'):
        return client.create_oco(params)
    return client.spot_signed('POST', '/api/v3/order/oco', params)


def _validate_oco_snapshot(client, pos, symbol, managed_quantity, step, open_orders):
    list_id = pos.get('oco_order_list_id')
    stored_ids = {str(value) for value in pos.get('oco_order_ids') or [] if value not in (None, '')}
    if list_id in (None, ''):
        if open_orders:
            return None, 'UNTRACKED_SPOT_OPEN_ORDERS'
        return None, None
    try:
        canonical = _get_order_list(client, {'orderListId': int(list_id)})
    except Exception as exc:
        return None, f'OCO_LOOKUP_FAILED: {exc}'
    if not isinstance(canonical, dict) or str(canonical.get('orderListId')) != str(list_id):
        return None, 'OCO_ID_MISMATCH'
    if str(canonical.get('listOrderStatus') or '').upper() != 'EXECUTING':
        return None, 'OCO_NOT_EXECUTING'
    listed_ids = {
        str(item.get('orderId')) for item in canonical.get('orders') or []
        if isinstance(item, dict) and item.get('orderId') not in (None, '')
    }
    if not listed_ids or (stored_ids and listed_ids != stored_ids):
        return None, 'OCO_LEG_IDS_MISMATCH'
    legs = [row for row in open_orders if str((row or {}).get('orderListId')) == str(list_id)]
    if len(legs) != len(listed_ids):
        return None, 'OCO_OPEN_LEGS_MISMATCH'
    remaining = []
    for leg in legs:
        if (str(leg.get('orderId')) not in listed_ids
                or str(leg.get('symbol') or '').upper() != symbol
                or str(leg.get('side') or '').upper() != 'SELL'):
            return None, 'OCO_CONTRACT_MISMATCH'
        original = _decimal(leg.get('origQty'))
        executed = _decimal(leg.get('executedQty') or 0)
        if original is None or executed is None or original <= 0 or executed < 0 or executed > original:
            return None, 'OCO_QUANTITY_INVALID'
        remaining.append(original - executed)
    protected = min(remaining)
    if protected != managed_quantity:
        return None, 'OCO_QUANTITY_MISMATCH'
    limit_leg = next((row for row in legs if str(row.get('type') or '').upper() in {'LIMIT_MAKER', 'TAKE_PROFIT_LIMIT'}), None)
    stop_leg = next((row for row in legs if str(row.get('type') or '').upper() in {'STOP_LOSS_LIMIT', 'STOP_LOSS'}), None)
    if not limit_leg or not stop_leg:
        return None, 'OCO_LEG_TYPES_INVALID'
    price = _decimal(limit_leg.get('price'))
    stop_price = _decimal(stop_leg.get('stopPrice'))
    stop_limit = _decimal(stop_leg.get('price'))
    if any(value is None or value <= 0 for value in (price, stop_price, stop_limit)):
        return None, 'OCO_PRICES_INVALID'
    return {
        'order_list_id': int(list_id),
        'order_ids': sorted(listed_ids),
        'protected_quantity': protected,
        'price': price,
        'stop_price': stop_price,
        'stop_limit_price': stop_limit,
    }, None


def _oco_payload(symbol, quantity, price, stop_price, stop_limit_price):
    return {
        'symbol': symbol,
        'side': 'SELL',
        'quantity': format_decimal_quantity(quantity),
        'price': format_decimal_quantity(price),
        'stopPrice': format_decimal_quantity(stop_price),
        'stopLimitPrice': format_decimal_quantity(stop_limit_price),
        'stopLimitTimeInForce': 'GTC',
    }


def _accept_oco(pos, response, quantity):
    list_id = response.get('orderListId') if isinstance(response, dict) else None
    order_ids = [
        str(item.get('orderId')) for item in (response.get('orders') or [])
        if isinstance(item, dict) and item.get('orderId') not in (None, '')
    ] if isinstance(response, dict) else []
    if list_id in (None, '') or not order_ids:
        raise ValueError('incomplete OCO creation response')
    pos['quantity'] = float(quantity)
    pos['oco_order_list_id'] = str(list_id)
    pos['oco_order_ids'] = order_ids
    pos['recovery_pending'] = False
    pos.pop('protection_warning', None)
    pos.pop('partial_spot_recovery', None)


def _mark_unprotected(pos, quantity, status, error):
    pos['quantity'] = float(max(quantity, Decimal('0')))
    pos['oco_order_list_id'] = ''
    pos['oco_order_ids'] = []
    pos['recovery_pending'] = True
    pos['protection_warning'] = f'{status}: {error}'


def _restore_snapshot(client, pos, snapshot, quantity, filters, price):
    normalized, details = _normalize(quantity, filters, price, market=False)
    if normalized <= 0:
        return {'restored': False, 'unprotectable': True, 'details': details}
    payload = _oco_payload(
        str(pos.get('symbol') or '').upper(), normalized,
        snapshot['price'], snapshot['stop_price'], snapshot['stop_limit_price'],
    )
    validation = residuals.validate_spot_oco_payload_notional(payload, filters)
    if not validation.get('should_send_oco'):
        return {'restored': False, 'unprotectable': True, 'details': validation}
    try:
        response = _create_oco(client, payload)
        _accept_oco(pos, response, normalized)
    except Exception as exc:
        return {'restored': False, 'error': str(exc), 'payload': payload}
    return {'restored': True, 'quantity': float(normalized), 'payload': payload}


def _fill_evidence(order, client_order_id, requested, symbol=None):
    order = order if isinstance(order, dict) else {}
    executed = _decimal(order.get('executedQty')) or Decimal('0')
    quote = _decimal(order.get('cummulativeQuoteQty') or order.get('cumQuoteQty')) or Decimal('0')
    fills = order.get('fills') if isinstance(order.get('fills'), list) else []
    if executed <= 0:
        executed = sum((_decimal(row.get('qty')) or Decimal('0')) for row in fills if isinstance(row, dict))
    if quote <= 0:
        quote = sum(
            (_decimal(row.get('qty')) or Decimal('0')) * (_decimal(row.get('price')) or Decimal('0'))
            for row in fills if isinstance(row, dict)
        )
    returned_id = order.get('clientOrderId') or order.get('origClientOrderId')
    status = str(order.get('status') or '').upper()
    fill_price = quote / executed if quote > 0 and executed > 0 else None
    attributable = bool(
        returned_id == client_order_id
        and str(order.get('symbol') or '').upper() == str(symbol or '').upper()
        and str(order.get('side') or '').upper() == 'SELL'
        and str(order.get('type') or '').upper() == 'MARKET'
        and status in {'FILLED', 'EXPIRED'}
        and executed > 0
        and executed <= requested
        and fill_price is not None
        and fill_price > 0
    )
    return {'attributable': attributable, 'executed': executed, 'quote': quote,
            'fill_price': fill_price, 'status': status, 'order_id': order.get('orderId')}


def _client_order_id(pos, symbol):
    trade_id = str(pos.get('id') or pos.get('trade_id') or '')
    digest = hashlib.sha256(f'{trade_id}|{symbol}|partial-long-spot-v1'.encode('utf-8')).hexdigest()[:24]
    return f'pls_{digest}'


def attempt_partial_long_spot(client, pos, price):
    """Attempt exactly one managed partial SELL and restore only managed protection."""
    if is_spot_long_recovery_pending(pos):
        return _result('RECOVERY_PENDING_REQUIRES_RECONCILIATION')
    symbol = str(pos.get('symbol') or '').upper()
    managed = _decimal(pos.get('quantity'))
    px = _decimal(price)
    entry = _decimal(pos.get('entry_price'))
    tp = _decimal(pos.get('tp'))
    asset = _asset(symbol)
    if (str(pos.get('direction') or '').lower() != 'long' or not asset
            or managed is None or managed <= 0 or px is None or px <= 0
            or entry is None or entry <= 0 or tp is None or tp <= 0):
        return _result('INVALID_MANAGED_POSITION')
    try:
        filters = client.get_spot_filters(symbol)
        market_step, _, _, _ = _filters(filters, market=True)
        oco_step, _, _, _ = _filters(filters, market=False)
        managed = normalize_quantity_to_step(managed, market_step)
        split = compute_partial_and_remaining(managed, Decimal('0.5'), market_step)
        partial, partial_details = _normalize(split.requested_partial_normalized, filters, px, market=True)
        remaining, remaining_details = _normalize(split.remaining_quantity, filters, px, market=False)
        before = _balance(client.get_spot_account(), asset)
        open_orders = _open_orders(client, symbol)
    except Exception as exc:
        return _result('PREFLIGHT_FAILED', error=str(exc))
    if partial <= 0:
        return _result('PARTIAL_NOT_OPERABLE', details=partial_details)
    if remaining <= 0:
        return _result('REMAINING_NOT_OPERABLE', details=remaining_details)
    if before['total'] < managed:
        return _result('MANAGED_BALANCE_MISMATCH', managed=float(managed), observed=float(before['total']))
    excess_inventory = before['total'] - managed
    snapshot, snapshot_error = _validate_oco_snapshot(
        client, pos, symbol, managed, oco_step, open_orders,
    )
    if snapshot_error:
        return _result('INVALID_OCO_SNAPSHOT', error=snapshot_error)
    if snapshot is None and before['locked'] > 0:
        return _result('LOCKED_BALANCE_WITHOUT_CANONICAL_OCO')

    tick = _decimal(filters.get('tick_size'))
    if tick is None or tick <= 0:
        return _result('PREFLIGHT_FAILED', error='invalid tick_size')
    new_sl = _decimal(utils.round_tick(float(entry) * 1.003, float(tick)))
    new_tp = _decimal(utils.round_tick(float(tp), float(tick)))
    new_sl_limit = _decimal(utils.round_tick(float(new_sl) * 0.999, float(tick)))
    success_oco_payload = _oco_payload(symbol, remaining, new_tp, new_sl, new_sl_limit)
    success_oco_check = residuals.validate_spot_oco_payload_notional(success_oco_payload, filters)
    if not success_oco_check.get('should_send_oco'):
        return _result('REMAINING_OCO_NOT_OPERABLE', details=success_oco_check)

    client_order_id = _client_order_id(pos, symbol)
    sell_payload = {
        'symbol': symbol,
        'side': 'SELL',
        'type': 'MARKET',
        'quantity': format_decimal_quantity(partial),
        'newClientOrderId': client_order_id,
        'newOrderRespType': 'FULL',
    }
    if 'e' in sell_payload['quantity'].lower():
        return _result('INVALID_QUANTITY_SERIALIZATION')

    if snapshot:
        try:
            _cancel_order_list(client, {'symbol': symbol, 'orderListId': snapshot['order_list_id']})
            pos['oco_order_list_id'] = ''
            pos['oco_order_ids'] = []
        except Exception as exc:
            return _result('CANCEL_OCO_FAILED', error=str(exc))
    pos['partial_spot_recovery'] = {
        'kind': 'partial_long_spot_v1',
        'status': 'OCO_CANCELLED_BEFORE_SELL' if snapshot else 'UNPROTECTED_BEFORE_SELL',
        'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'client_order_id': client_order_id,
        'order_id': None,
        'attempted_quantity': format_decimal_quantity(partial),
        'managed_before': format_decimal_quantity(managed),
        'excess_before': format_decimal_quantity(excess_inventory),
        'total_before': format_decimal_quantity(before['total']),
        'oco_snapshot': {
            key: format_decimal_quantity(value) if isinstance(value, Decimal) else value
            for key, value in (snapshot or {}).items()
        } if snapshot else None,
        'success_oco_payload': dict(success_oco_payload),
        'executed_known': '0',
    }
    try:
        released = _balance(client.get_spot_account(), asset)
    except Exception as exc:
        _mark_unprotected(pos, managed, 'BALANCE_AFTER_CANCEL_UNKNOWN', exc)
        return _result('BALANCE_AFTER_CANCEL_UNKNOWN', error=str(exc))
    if released['free'] < managed or released['total'] < managed:
        restore = (_restore_snapshot(client, pos, snapshot, min(managed, released['free']), filters, px)
                   if snapshot else {'restored': False})
        if not restore.get('restored'):
            _mark_unprotected(pos, min(managed, released['total']), 'BALANCE_NOT_RELEASED', 'insufficient released managed balance')
        return _result('BALANCE_NOT_RELEASED', restore=restore)

    order = None
    post_error = None
    deterministic_failure = False
    try:
        order = _create_order(client, sell_payload)
    except Exception as exc:
        post_error = str(exc)
        deterministic_failure = isinstance(exc, HTTPError) and 400 <= int(exc.code) < 500 and int(exc.code) != 429
    evidence_order = dict(order) if isinstance(order, dict) else {}
    lookup = {'symbol': symbol}
    if evidence_order.get('orderId') not in (None, ''):
        lookup['orderId'] = evidence_order['orderId']
    else:
        lookup['origClientOrderId'] = client_order_id
    lookup_error = None
    try:
        refreshed = _get_order(client, lookup)
        if isinstance(refreshed, dict):
            evidence_order.update(refreshed)
    except Exception as exc:
        lookup_error = str(exc)
    evidence = _fill_evidence(evidence_order, client_order_id, partial, symbol)
    recovery = pos['partial_spot_recovery']
    recovery['order_id'] = evidence['order_id']
    recovery['executed_known'] = format_decimal_quantity(evidence['executed'])
    try:
        after = _balance(client.get_spot_account(), asset)
    except Exception as exc:
        _mark_unprotected(pos, managed, 'POST_BALANCE_UNKNOWN', exc)
        return _result('POST_BALANCE_UNKNOWN', post_error=post_error, lookup_error=lookup_error)

    provable_remaining = normalize_quantity_to_step(
        min(managed, max(after['total'] - excess_inventory, Decimal('0'))), market_step,
    )
    if not evidence['attributable']:
        if deterministic_failure and provable_remaining == managed and snapshot:
            restore = _restore_snapshot(client, pos, snapshot, managed, filters, px)
            if restore.get('restored'):
                return _result('SELL_FAILED_PROTECTED', post_error=post_error, lookup_error=lookup_error,
                               restore=restore, sell_payload=sell_payload)
            _mark_unprotected(pos, managed, 'SELL_FAILED_RECOVERY_FAILED', restore.get('error'))
            return _result('SELL_FAILED_RECOVERY_FAILED', post_error=post_error, restore=restore,
                           sell_payload=sell_payload)
        _mark_unprotected(pos, provable_remaining, 'AMBIGUOUS_SELL', post_error or lookup_error or 'missing fill evidence')
        recovery['status'] = 'AMBIGUOUS_SELL'
        return _result('AMBIGUOUS_SELL', post_error=post_error, lookup_error=lookup_error,
                       provable_remaining=float(provable_remaining), sell_payload=sell_payload)

    try:
        exact_remaining = remaining_after_execution(managed, evidence['executed'], market_step)
    except ValueError as exc:
        _mark_unprotected(pos, provable_remaining, 'INVALID_EXECUTION_EVIDENCE', exc)
        return _result('INVALID_EXECUTION_EVIDENCE', error=str(exc), sell_payload=sell_payload)
    if provable_remaining != exact_remaining:
        _mark_unprotected(pos, min(provable_remaining, exact_remaining), 'BALANCE_EXECUTION_MISMATCH', 'fresh balance disagrees with fill')
        return _result('BALANCE_EXECUTION_MISMATCH', sell_payload=sell_payload)

    operable_remaining, residual_details = _normalize(exact_remaining, filters, px, market=False)
    restore = None
    residual_recorded = False
    if operable_remaining > 0:
        final_payload = dict(success_oco_payload)
        final_payload['quantity'] = format_decimal_quantity(operable_remaining)
        try:
            response = _create_oco(client, final_payload)
            _accept_oco(pos, response, operable_remaining)
            pos['sl'] = float(new_sl)
            restore = {'restored': True, 'payload': final_payload}
        except Exception as exc:
            _mark_unprotected(pos, operable_remaining, 'PARTIAL_OCO_FAILED', exc)
            restore = {'restored': False, 'error': str(exc), 'payload': final_payload}
    elif exact_remaining > 0:
        try:
            residual_recorded = bool(residuals.handle_unprotectable_spot_residual(
                symbol, asset, float(exact_remaining), float(px), filters,
                reason=residual_details.get('reason') or 'partial_spot_residual',
            ))
        except Exception:
            residual_recorded = False
        pos['quantity'] = float(exact_remaining)
        pos['oco_order_list_id'] = ''
        pos['oco_order_ids'] = []
        pos['recovery_pending'] = not residual_recorded
    else:
        pos['quantity'] = 0.0

    return {
        'status': 'PARTIAL_EXECUTED_PROTECTED' if restore and restore.get('restored') else 'PARTIAL_EXECUTED_UNPROTECTED',
        'confirmed_execution': True,
        'requested_quantity': float(partial),
        'executed_quantity': float(evidence['executed']),
        'remaining_quantity': float(exact_remaining),
        'fill_price': float(evidence['fill_price'] or px),
        'quote_quantity': float(evidence['quote']),
        'order_id': evidence['order_id'],
        'client_order_id': client_order_id,
        'sell_payload': sell_payload,
        'restore': restore,
        'residual_recorded': residual_recorded,
        'excess_inventory': float(excess_inventory),
    }


def reconcile_pending_partial_long_spot(client, pos):
    """Resolve only a recorded partial SELL, never submit another SELL."""
    if not is_spot_long_recovery_pending(pos):
        return _result('NO_RECOVERY_LOCK')
    recovery = pos.get('partial_spot_recovery')
    if not isinstance(recovery, dict) or recovery.get('kind') != 'partial_long_spot_v1':
        return _result('RECOVERY_EVIDENCE_MISSING')
    symbol = str(pos.get('symbol') or '').upper()
    asset = _asset(symbol)
    managed = _decimal(recovery.get('managed_before'))
    requested = _decimal(recovery.get('attempted_quantity'))
    excess = _decimal(recovery.get('excess_before'))
    total_before = _decimal(recovery.get('total_before'))
    client_order_id = recovery.get('client_order_id')
    if (not asset or not client_order_id or managed is None or managed <= 0
            or requested is None or requested <= 0 or requested > managed
            or excess is None or excess < 0 or total_before != managed + excess):
        return _result('RECOVERY_EVIDENCE_INVALID')

    params = {'symbol': symbol, 'origClientOrderId': client_order_id}
    try:
        order = _get_order(client, params)
        filters = client.get_spot_filters(symbol)
        price = _decimal(client.get_spot_price(symbol))
        balance = _balance(client.get_spot_account(), asset)
        open_orders = _open_orders(client, symbol)
        market_step, _, _, _ = _filters(filters, market=True)
        if price is None or price <= 0 or not isinstance(order, dict):
            raise ValueError('incomplete fresh exchange evidence')
    except Exception as exc:
        recovery['status'] = 'REQUERY_FAILED'
        pos['protection_warning'] = f'REQUERY_FAILED: {exc}'
        return _result('REQUERY_FAILED', error=str(exc))

    status = str(order.get('status') or '').upper()
    executed = _decimal(order.get('executedQty'))
    original = _decimal(order.get('origQty'))
    if (order.get('clientOrderId') != client_order_id
            or str(order.get('symbol') or '').upper() != symbol
            or str(order.get('side') or '').upper() != 'SELL'
            or str(order.get('type') or '').upper() != 'MARKET'
            or original != requested
            or executed is None or executed < 0 or executed > requested
            or (recovery.get('order_id') not in (None, '')
                and str(order.get('orderId')) != str(recovery['order_id']))):
        recovery['status'] = 'ORDER_EVIDENCE_MISMATCH'
        return _result('ORDER_EVIDENCE_MISMATCH')
    recovery['order_id'] = order.get('orderId')
    recovery['executed_known'] = format_decimal_quantity(executed)
    if executed % market_step != 0:
        recovery['status'] = 'EXECUTION_STEP_MISMATCH'
        return _result('EXECUTION_STEP_MISMATCH')
    remaining = remaining_after_execution(managed, executed, market_step)
    if balance['total'] != total_before - executed:
        recovery['status'] = 'BALANCE_OR_ORDER_MISMATCH'
        return _result('BALANCE_OR_ORDER_MISMATCH')

    if status == 'PARTIALLY_FILLED':
        pos['quantity'] = float(remaining)
        recovery['status'] = 'ORDER_STILL_PARTIALLY_FILLED'
        return _result('ORDER_STILL_PARTIALLY_FILLED', remaining_quantity=float(remaining))

    if balance['free'] < remaining or open_orders:
        recovery['status'] = 'BALANCE_OR_ORDER_MISMATCH'
        return _result('BALANCE_OR_ORDER_MISMATCH')

    if status in {'REJECTED', 'CANCELED', 'EXPIRED'} and executed == 0:
        snapshot = recovery.get('oco_snapshot')
        if not isinstance(snapshot, dict):
            recovery['status'] = 'CANONICAL_OCO_SNAPSHOT_MISSING'
            return _result('CANONICAL_OCO_SNAPSHOT_MISSING')
        exact, _ = _normalize(managed, filters, price, market=False)
        if exact != managed:
            recovery['status'] = 'OCO_NOT_EXACT'
            return _result('OCO_NOT_EXACT')
        restore = _restore_snapshot(client, pos, snapshot, managed, filters, price)
        if not restore.get('restored'):
            recovery['status'] = 'OCO_RESTORE_FAILED'
            return _result('OCO_RESTORE_FAILED', restore=restore)
        return _result('REJECTED_ZERO_EXECUTION_PROTECTED', restore=restore)

    evidence = _fill_evidence(order, client_order_id, requested, symbol)
    if status not in {'FILLED', 'EXPIRED'} or not evidence['attributable']:
        recovery['status'] = 'ORDER_EXECUTION_UNRESOLVED'
        return _result('ORDER_EXECUTION_UNRESOLVED')

    operable, details = _normalize(remaining, filters, price, market=False)
    if operable <= 0:
        try:
            residuals.handle_unprotectable_spot_residual(
                symbol, asset, float(remaining), float(price), filters,
                reason=details.get('reason') or 'partial_spot_residual',
            )
        except Exception:
            pass
        recovery['status'] = 'DUST_RESIDUAL_PENDING'
        pos['quantity'] = float(remaining)
        return _result('DUST_RESIDUAL_PENDING', remaining_quantity=float(remaining))
    if operable != remaining:
        recovery['status'] = 'OCO_NOT_EXACT'
        return _result('OCO_NOT_EXACT', remaining_quantity=float(remaining))
    payload = recovery.get('success_oco_payload')
    if not isinstance(payload, dict) or payload.get('symbol') != symbol or payload.get('side') != 'SELL':
        recovery['status'] = 'OCO_PAYLOAD_MISSING'
        return _result('OCO_PAYLOAD_MISSING')
    payload = dict(payload, quantity=format_decimal_quantity(operable))
    if not residuals.validate_spot_oco_payload_notional(payload, filters).get('should_send_oco'):
        recovery['status'] = 'OCO_NOT_OPERABLE'
        return _result('OCO_NOT_OPERABLE')
    try:
        response = _create_oco(client, payload)
        _accept_oco(pos, response, operable)
    except Exception as exc:
        recovery['status'] = 'OCO_RESTORE_FAILED'
        pos['protection_warning'] = f'OCO_RESTORE_FAILED: {exc}'
        return _result('OCO_RESTORE_FAILED', error=str(exc))
    pos['sl'] = float(_decimal(payload['stopPrice']))
    return {
        'status': 'CONFIRMED_PARTIAL_PROTECTED',
        'confirmed_execution': not pos.get('partial_taken'),
        'executed_quantity': float(executed),
        'remaining_quantity': float(remaining),
        'fill_price': float(evidence['fill_price']),
        'order_id': order.get('orderId'),
    }
