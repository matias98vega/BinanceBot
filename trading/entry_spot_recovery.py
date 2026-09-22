"""Evidence-gated recovery for an unprotected Spot LONG created at entry."""

import hashlib
import time
from decimal import Decimal

import partial_spot_long as spot
import residuals
import utils
from quantity_integrity import format_decimal_quantity, remaining_after_execution
from spot_recovery_lock import is_spot_long_recovery_pending


KIND = 'entry_protection_v1'


def _result(status, **details):
    return {'status': status, 'confirmed_flat': False, **details}


def _client_order_id(pos):
    source = f"{pos.get('id')}|{pos.get('symbol')}|entry-emergency-sell-v1"
    return 'ees_' + hashlib.sha256(source.encode('utf-8')).hexdigest()[:24]


def prepare_entry_recovery(client, pos, buy_order, managed_quantity):
    """Capture the managed balance before any emergency SELL can be submitted."""
    symbol = str(pos.get('symbol') or '').upper()
    managed = spot._decimal(managed_quantity)
    buy_executed = spot._decimal((buy_order or {}).get('executedQty'))
    recovery = {
        'kind': KIND,
        'status': 'ENTRY_EVIDENCE_UNKNOWN',
        'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'buy_order_id': (buy_order or {}).get('orderId'),
        'buy_client_order_id': (buy_order or {}).get('clientOrderId'),
        'buy_executed': format_decimal_quantity(buy_executed) if buy_executed is not None and buy_executed >= 0 else None,
        'managed_before': format_decimal_quantity(managed) if managed is not None and managed >= 0 else None,
        'total_before': None,
        'excess_before': None,
        'emergency_client_order_id': _client_order_id(pos),
        'emergency_order_id': None,
        'emergency_quantity': format_decimal_quantity(managed) if managed is not None and managed >= 0 else None,
        'sell_attempted': False,
        'executed_known': '0',
    }
    pos['entry_spot_recovery'] = recovery
    if (not symbol.endswith('USDT')
            or str((buy_order or {}).get('symbol') or '').upper() != symbol
            or str((buy_order or {}).get('side') or '').upper() != 'BUY'
            or str((buy_order or {}).get('status') or '').upper() != 'FILLED'
            or managed is None or managed <= 0
            or buy_executed is None or buy_executed < managed
            or (recovery['buy_order_id'] in (None, '') and not recovery['buy_client_order_id'])):
        recovery['status'] = 'ENTRY_QUANTITY_NOT_OPERABLE'
        return _result('ENTRY_QUANTITY_NOT_OPERABLE')
    try:
        balance = spot._balance(client.get_spot_account(), spot._asset(symbol))
        open_orders = spot._open_orders(client, symbol)
    except Exception:
        recovery['status'] = 'ENTRY_EXCHANGE_EVIDENCE_UNKNOWN'
        return _result('ENTRY_EXCHANGE_EVIDENCE_UNKNOWN')
    if balance['total'] < managed or balance['free'] < managed or open_orders:
        recovery['status'] = 'ENTRY_BALANCE_OR_OCO_UNKNOWN'
        return _result('ENTRY_BALANCE_OR_OCO_UNKNOWN')
    recovery['total_before'] = format_decimal_quantity(balance['total'])
    recovery['excess_before'] = format_decimal_quantity(balance['total'] - managed)
    recovery['status'] = 'READY_FOR_EMERGENCY_SELL'
    return _result('READY_FOR_EMERGENCY_SELL')


def submit_entry_emergency_sell(client, pos):
    """Send at most one identified emergency SELL; never retry an unknown POST."""
    recovery = pos.get('entry_spot_recovery')
    if (not is_spot_long_recovery_pending(pos) or not isinstance(recovery, dict)
            or recovery.get('kind') != KIND or recovery.get('status') != 'READY_FOR_EMERGENCY_SELL'
            or recovery.get('sell_attempted')):
        return _result('ENTRY_SELL_NOT_AUTHORIZED')
    symbol = str(pos['symbol']).upper()
    payload = {
        'symbol': symbol, 'side': 'SELL', 'type': 'MARKET',
        'quantity': recovery['emergency_quantity'],
        'newClientOrderId': recovery['emergency_client_order_id'],
        'newOrderRespType': 'FULL',
    }
    recovery['sell_attempted'] = True
    recovery['status'] = 'EMERGENCY_SELL_OUTCOME_UNKNOWN'
    try:
        response = spot._create_order(client, payload)
        if isinstance(response, dict):
            recovery['emergency_order_id'] = response.get('orderId')
    except Exception as exc:
        recovery['post_error_type'] = type(exc).__name__
    # An immediate GET may prove a complete emergency exit. Otherwise the
    # persisted clientOrderId is the only authorized route on a later cycle.
    return reconcile_pending_entry_spot_long(client, pos, restore_protection=False)


def _validate_order(order, recovery, symbol, requested):
    if not isinstance(order, dict):
        return None, None, 'ORDER_EVIDENCE_MISMATCH'
    executed = spot._decimal(order.get('executedQty'))
    original = spot._decimal(order.get('origQty'))
    if (order.get('clientOrderId') != recovery.get('emergency_client_order_id')
            or str(order.get('symbol') or '').upper() != symbol
            or str(order.get('side') or '').upper() != 'SELL'
            or str(order.get('type') or '').upper() != 'MARKET'
            or original != requested or executed is None or executed < 0 or executed > requested
            or (recovery.get('emergency_order_id') not in (None, '')
                and str(order.get('orderId')) != str(recovery['emergency_order_id']))):
        return None, None, 'ORDER_EVIDENCE_MISMATCH'
    return str(order.get('status') or '').upper(), executed, None


def _confirm_existing_oco(client, pos, symbol, managed, filters, open_orders):
    try:
        step, _, _, _ = spot._filters(filters, market=False)
        snapshot, error = spot._validate_oco_snapshot(client, pos, symbol, managed, step, open_orders)
    except Exception:
        return False
    return snapshot is not None and error is None


def _restore_managed_oco(client, pos, recovery, managed, filters, price):
    symbol = str(pos['symbol']).upper()
    exact, _ = spot._normalize(managed, filters, price, market=False)
    if exact != managed:
        return _result('ENTRY_OCO_NOT_EXACT')
    tick = spot._decimal(filters.get('tick_size'))
    tp = spot._decimal(pos.get('tp'))
    sl = spot._decimal(pos.get('sl'))
    if any(value is None or value <= 0 for value in (tick, tp, sl)):
        return _result('ENTRY_PROTECTION_TERMS_INVALID')
    payload = spot._oco_payload(
        symbol, managed,
        spot._decimal(utils.round_tick(float(tp), float(tick))),
        spot._decimal(utils.round_tick(float(sl), float(tick))),
        spot._decimal(utils.round_tick(float(sl) * 0.999, float(tick))),
    )
    if not residuals.validate_spot_oco_payload_notional(payload, filters).get('should_send_oco'):
        return _result('ENTRY_OCO_NOT_OPERABLE')
    try:
        response = spot._create_oco(client, payload)
        list_id = response.get('orderListId') if isinstance(response, dict) else None
        order_ids = [str(item['orderId']) for item in (response.get('orders') or [])
                     if isinstance(item, dict) and item.get('orderId') not in (None, '')] if isinstance(response, dict) else []
        if list_id in (None, '') or len(order_ids) != 2:
            raise ValueError('incomplete OCO creation response')
        pos['oco_order_list_id'] = str(list_id)
        pos['oco_order_ids'] = order_ids
    except Exception as exc:
        recovery['status'] = 'ENTRY_OCO_CREATE_UNCONFIRMED'
        return _result('ENTRY_OCO_CREATE_UNCONFIRMED', error_type=type(exc).__name__)
    recovery['status'] = 'ENTRY_OCO_AWAITING_CONFIRMATION'
    try:
        open_orders = spot._open_orders(client, symbol)
    except Exception:
        return _result('ENTRY_OCO_AWAITING_CONFIRMATION')
    if not _confirm_existing_oco(client, pos, symbol, managed, filters, open_orders):
        return _result('ENTRY_OCO_AWAITING_CONFIRMATION')
    pos['quantity'] = float(managed)
    pos['recovery_pending'] = False
    pos.pop('protection_warning', None)
    pos.pop('entry_spot_recovery', None)
    return _result('ENTRY_PROTECTED')


def reconcile_pending_entry_spot_long(client, pos, *, restore_protection=True):
    """Resolve only a recorded entry emergency SELL or proven zero-exit state."""
    if not is_spot_long_recovery_pending(pos):
        return _result('NO_RECOVERY_LOCK')
    recovery = pos.get('entry_spot_recovery')
    if not isinstance(recovery, dict) or recovery.get('kind') != KIND:
        return _result('ENTRY_RECOVERY_EVIDENCE_MISSING')
    symbol = str(pos.get('symbol') or '').upper()
    managed = spot._decimal(recovery.get('managed_before'))
    total_before = spot._decimal(recovery.get('total_before'))
    excess = spot._decimal(recovery.get('excess_before'))
    requested = spot._decimal(recovery.get('emergency_quantity'))
    buy_executed = spot._decimal(recovery.get('buy_executed'))
    if (not symbol.endswith('USDT') or managed is None or managed <= 0
            or requested != managed or buy_executed is None or buy_executed < managed
            or total_before is None or excess is None or excess < 0
            or total_before != managed + excess):
        return _result('ENTRY_RECOVERY_EVIDENCE_INVALID')
    buy_lookup = {'symbol': symbol}
    if recovery.get('buy_order_id') not in (None, ''):
        buy_lookup['orderId'] = recovery['buy_order_id']
    elif recovery.get('buy_client_order_id'):
        buy_lookup['origClientOrderId'] = recovery['buy_client_order_id']
    else:
        return _result('ENTRY_BUY_EVIDENCE_MISSING')
    if not recovery.get('sell_attempted') or not recovery.get('emergency_client_order_id'):
        return _result('ENTRY_EXIT_EVIDENCE_UNKNOWN')
    try:
        buy = spot._get_order(client, buy_lookup)
        order = spot._get_order(client, {
            'symbol': symbol, 'origClientOrderId': recovery['emergency_client_order_id'],
        })
        filters = client.get_spot_filters(symbol)
        price = spot._decimal(client.get_spot_price(symbol))
        balance = spot._balance(client.get_spot_account(), spot._asset(symbol))
        open_orders = spot._open_orders(client, symbol)
        market_step, _, _, _ = spot._filters(filters, market=True)
        if price is None or price <= 0:
            raise ValueError('invalid current price')
    except Exception as exc:
        recovery['status'] = 'ENTRY_REQUERY_FAILED'
        return _result('ENTRY_REQUERY_FAILED', error_type=type(exc).__name__)
    if (not isinstance(buy, dict)
            or str(buy.get('symbol') or '').upper() != symbol
            or str(buy.get('side') or '').upper() != 'BUY'
            or str(buy.get('status') or '').upper() != 'FILLED'
            or spot._decimal(buy.get('executedQty')) != buy_executed
            or (recovery.get('buy_order_id') not in (None, '')
                and str(buy.get('orderId')) != str(recovery['buy_order_id']))
            or (recovery.get('buy_client_order_id')
                and buy.get('clientOrderId') != recovery['buy_client_order_id'])):
        recovery['status'] = 'ENTRY_BUY_EVIDENCE_MISMATCH'
        return _result('ENTRY_BUY_EVIDENCE_MISMATCH')
    status, executed, error = _validate_order(order, recovery, symbol, requested)
    if error:
        recovery['status'] = error
        return _result(error)
    recovery['emergency_order_id'] = order.get('orderId')
    recovery['executed_known'] = format_decimal_quantity(executed)
    if executed % market_step != 0:
        recovery['status'] = 'ENTRY_EXECUTION_STEP_MISMATCH'
        return _result('ENTRY_EXECUTION_STEP_MISMATCH')
    remaining = remaining_after_execution(managed, executed, market_step)
    if balance['total'] != total_before - executed:
        recovery['status'] = 'ENTRY_BALANCE_MISMATCH'
        return _result('ENTRY_BALANCE_MISMATCH')
    if status == 'PARTIALLY_FILLED':
        pos['quantity'] = float(remaining)
        recovery['status'] = 'ENTRY_EXIT_PARTIALLY_FILLED'
        return _result('ENTRY_EXIT_PARTIALLY_FILLED', remaining_quantity=float(remaining))
    if status in {'FILLED', 'EXPIRED'} and executed > 0:
        pos['quantity'] = float(remaining)
        recovery['status'] = 'ENTRY_EXIT_CONFIRMED_MANUAL_FINALIZATION'
        return _result('ENTRY_EXIT_CONFIRMED_MANUAL_FINALIZATION',
                       confirmed_flat=(remaining == 0 and not open_orders),
                       remaining_quantity=float(remaining))
    if status not in {'REJECTED', 'CANCELED', 'EXPIRED'} or executed != 0:
        recovery['status'] = 'ENTRY_EXIT_OUTCOME_UNRESOLVED'
        return _result('ENTRY_EXIT_OUTCOME_UNRESOLVED')
    recovery['status'] = 'ENTRY_EXIT_ZERO_CONFIRMED'
    if not restore_protection:
        return _result('ENTRY_EXIT_ZERO_CONFIRMED')
    if pos.get('oco_order_list_id') not in (None, ''):
        if _confirm_existing_oco(client, pos, symbol, managed, filters, open_orders):
            pos['quantity'] = float(managed)
            pos['recovery_pending'] = False
            pos.pop('protection_warning', None)
            pos.pop('entry_spot_recovery', None)
            return _result('ENTRY_PROTECTED')
        recovery['status'] = 'ENTRY_OCO_AWAITING_CONFIRMATION'
        return _result('ENTRY_OCO_AWAITING_CONFIRMATION')
    if open_orders or balance['free'] < managed:
        recovery['status'] = 'ENTRY_BALANCE_OR_OCO_UNKNOWN'
        return _result('ENTRY_BALANCE_OR_OCO_UNKNOWN')
    return _restore_managed_oco(client, pos, recovery, managed, filters, price)
