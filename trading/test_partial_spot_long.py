#!/usr/bin/env python3
import io
import json
import os
import sys
import unittest
from copy import deepcopy
from decimal import Decimal
from unittest.mock import Mock, patch
from urllib.error import HTTPError

sys.path.insert(0, os.path.dirname(__file__))

import partial_spot_long
from quantity_integrity import format_decimal_quantity
from orchestration import position_lifecycle


def _http_error():
    return HTTPError('https://offline.invalid/order', 400, 'Bad Request', {},
                     io.BytesIO(b'{"code":-1100,"msg":"illegal characters"}'))


class OfflinePartialClient:
    def __init__(self, managed='0.00016', extra='0.00002783'):
        self.managed = Decimal(managed)
        self.extra = Decimal(extra)
        self.total = self.managed + self.extra
        self.cancelled = False
        self.order_calls = []
        self.oco_calls = []
        self.cancel_calls = []
        self.lookup_calls = []
        self.sell_mode = 'success'
        self.oco_error = None
        self.lookup_error = None
        self.executed = Decimal('0.00008')
        self.last_order = None
        self.filters = {
            'status': 'TRADING', 'step_size': '0.00001', 'min_qty': '0.00001',
            'max_qty': '100', 'market_step_size': '0.00001',
            'market_min_qty': '0.00001', 'market_max_qty': '100',
            'min_notional': '5', 'tick_size': '0.01',
        }

    def get_spot_filters(self, symbol):
        return dict(self.filters)

    def get_spot_price(self, symbol):
        return getattr(self, 'price', 110000.0)

    def get_spot_account(self):
        free = self.total if self.cancelled else self.extra
        locked = Decimal('0') if self.cancelled else self.managed
        return {'balances': [{'asset': 'BTC', 'free': str(free), 'locked': str(locked)}]}

    def spot_open_orders(self, params):
        if self.cancelled:
            return []
        return [
            {'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'LIMIT_MAKER',
             'orderListId': 77, 'orderId': 78, 'origQty': str(self.managed),
             'executedQty': '0', 'price': '120000', 'stopPrice': '0'},
            {'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'STOP_LOSS_LIMIT',
             'orderListId': 77, 'orderId': 79, 'origQty': str(self.managed),
             'executedQty': '0', 'price': '89000', 'stopPrice': '90000'},
        ]

    def get_order_list(self, params):
        return {'orderListId': 77, 'listOrderStatus': 'EXECUTING',
                'orders': [{'orderId': 78}, {'orderId': 79}]}

    def cancel_order_list(self, params):
        self.cancel_calls.append(dict(params))
        self.cancelled = True
        return {'orderListId': 77}

    def _fill(self, params):
        self.total -= self.executed
        self.last_order = {
            'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET', 'status': 'FILLED',
            'orderId': 500, 'clientOrderId': params['newClientOrderId'],
            'origQty': params['quantity'],
            'executedQty': str(self.executed),
            'cummulativeQuoteQty': str(self.executed * Decimal('100000')),
        }
        return deepcopy(self.last_order)

    def create_spot_order(self, params):
        self.order_calls.append(dict(params))
        if self.sell_mode == 'http_error':
            raise _http_error()
        if self.sell_mode == 'timeout_executed':
            self._fill(params)
            raise TimeoutError('offline timeout after exchange acceptance')
        if self.sell_mode == 'timeout_unknown':
            raise TimeoutError('offline timeout before evidence')
        return self._fill(params)

    def get_spot_order(self, params):
        self.lookup_calls.append(dict(params))
        if self.lookup_error or self.last_order is None:
            raise RuntimeError(self.lookup_error or 'offline order not found')
        return deepcopy(self.last_order)

    def create_oco(self, params):
        self.oco_calls.append(dict(params))
        if self.oco_error:
            raise RuntimeError(self.oco_error)
        return {'orderListId': 900, 'orders': [{'orderId': 901}, {'orderId': 902}]}


def _position(quantity=0.00016):
    return {
        'id': 'long_BTCUSDT_test', 'direction': 'long', 'symbol': 'BTCUSDT',
        'entry_price': 90000.0, 'quantity': quantity, 'tp': 120000.0, 'sl': 80000.0,
        'oco_order_list_id': '77', 'oco_order_ids': ['78', '79'], 'partial_taken': False,
    }


class PartialSpotLongSafetyTests(unittest.TestCase):
    def _ambiguous_position(self, client=None):
        client = client or OfflinePartialClient()
        client.sell_mode = 'timeout_unknown'
        pos = _position()
        result = partial_spot_long.attempt_partial_long_spot(client, pos, 110000)
        self.assertEqual(result['status'], 'AMBIGUOUS_SELL')
        self.assertTrue(pos['recovery_pending'])
        return client, pos

    def _rejected_order(self, client):
        client.last_order = {
            'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET',
            'status': 'REJECTED', 'orderId': 501,
            'clientOrderId': client.order_calls[0]['newClientOrderId'],
            'origQty': client.order_calls[0]['quantity'],
            'executedQty': '0', 'cummulativeQuoteQty': '0',
        }

    def test_r7_filled_requery_restores_managed_residual_and_unlocks(self):
        client, pos = self._ambiguous_position()
        client._fill(client.order_calls[0])
        result = partial_spot_long.reconcile_pending_partial_long_spot(client, pos)
        self.assertEqual(result['status'], 'CONFIRMED_PARTIAL_PROTECTED')
        self.assertTrue(result['confirmed_execution'])
        self.assertEqual(pos['quantity'], 0.00008)
        self.assertFalse(pos['recovery_pending'])
        self.assertEqual(client.oco_calls[-1]['quantity'], '0.00008')

    def test_r8_partial_status_preserves_exact_residual_and_lock(self):
        client, pos = self._ambiguous_position()
        client.executed = Decimal('0.00003')
        client._fill(client.order_calls[0])
        client.last_order['status'] = 'PARTIALLY_FILLED'
        client.spot_open_orders = lambda params: [deepcopy(client.last_order)]
        result = partial_spot_long.reconcile_pending_partial_long_spot(client, pos)
        self.assertEqual(result['status'], 'ORDER_STILL_PARTIALLY_FILLED')
        self.assertEqual(pos['quantity'], 0.00013)
        self.assertTrue(pos['recovery_pending'])
        self.assertFalse(client.oco_calls)

    def test_r9_rejected_zero_execution_restores_snapshot_then_unlocks(self):
        client, pos = self._ambiguous_position()
        self._rejected_order(client)
        result = partial_spot_long.reconcile_pending_partial_long_spot(client, pos)
        self.assertEqual(result['status'], 'REJECTED_ZERO_EXECUTION_PROTECTED')
        self.assertFalse(pos['recovery_pending'])
        self.assertEqual(pos['quantity'], 0.00016)
        self.assertEqual(client.oco_calls[-1]['quantity'], '0.00016')
        self.assertEqual(client.total, Decimal('0.00018783'))

    def test_zero_execution_cannot_unlock_with_smaller_oco_quantity(self):
        client, pos = self._ambiguous_position()
        self._rejected_order(client)
        client.filters['max_qty'] = '0.00015'
        result = partial_spot_long.reconcile_pending_partial_long_spot(client, pos)
        self.assertEqual(result['status'], 'OCO_NOT_EXACT')
        self.assertTrue(pos['recovery_pending'])
        self.assertFalse(client.oco_calls)

    def test_filled_cannot_unlock_with_smaller_oco_quantity(self):
        client, pos = self._ambiguous_position()
        client._fill(client.order_calls[0])
        client.filters['max_qty'] = '0.00007'
        result = partial_spot_long.reconcile_pending_partial_long_spot(client, pos)
        self.assertEqual(result['status'], 'OCO_NOT_EXACT')
        self.assertTrue(pos['recovery_pending'])
        self.assertFalse(client.oco_calls)

    def test_r10_query_failure_keeps_lock(self):
        client, pos = self._ambiguous_position()
        result = partial_spot_long.reconcile_pending_partial_long_spot(client, pos)
        self.assertEqual(result['status'], 'REQUERY_FAILED')
        self.assertTrue(pos['recovery_pending'])
        self.assertFalse(client.oco_calls)

    def test_order_quantity_mismatch_cannot_unlock(self):
        client, pos = self._ambiguous_position()
        client._fill(client.order_calls[0])
        client.last_order['origQty'] = '0.00009'
        result = partial_spot_long.reconcile_pending_partial_long_spot(client, pos)
        self.assertEqual(result['status'], 'ORDER_EVIDENCE_MISMATCH')
        self.assertTrue(pos['recovery_pending'])
        self.assertFalse(client.oco_calls)

    def test_r11_oco_restore_failure_keeps_lock(self):
        client, pos = self._ambiguous_position()
        self._rejected_order(client)
        client.oco_error = 'offline restore failed'
        result = partial_spot_long.reconcile_pending_partial_long_spot(client, pos)
        self.assertEqual(result['status'], 'OCO_RESTORE_FAILED')
        self.assertTrue(pos['recovery_pending'])

    def test_r12_r13_extra_inventory_never_enters_recovery_oco(self):
        client, pos = self._ambiguous_position()
        self._rejected_order(client)
        partial_spot_long.reconcile_pending_partial_long_spot(client, pos)
        self.assertEqual(client.oco_calls[-1]['quantity'], '0.00016')
        self.assertNotEqual(client.oco_calls[-1]['quantity'], '0.00018783')

    def test_r17_real_filter_price_transition_to_dust(self):
        client, pos = self._ambiguous_position()
        client._fill(client.order_calls[0])
        client.price = 60000.0
        with patch.object(partial_spot_long.residuals, 'handle_unprotectable_spot_residual', return_value=True) as handler:
            result = partial_spot_long.reconcile_pending_partial_long_spot(client, pos)
        self.assertEqual(result['status'], 'DUST_RESIDUAL_PENDING')
        self.assertEqual(pos['quantity'], 0.00008)
        self.assertTrue(pos['recovery_pending'])
        self.assertEqual(handler.call_args.args[2], 0.00008)
        self.assertFalse(client.oco_calls)

    def test_r22_recovery_metadata_is_json_persistable(self):
        _, pos = self._ambiguous_position()
        persisted = json.loads(json.dumps({'positions': [pos]}))
        metadata = persisted['positions'][0]['partial_spot_recovery']
        self.assertEqual(metadata['attempted_quantity'], '0.00008')
        self.assertEqual(metadata['managed_before'], '0.00016')
        self.assertEqual(metadata['excess_before'], '0.00002783')
        self.assertTrue(metadata['started_at'])
        self.assertEqual(metadata['oco_snapshot']['protected_quantity'], '0.00016')

    def test_balance_race_from_external_inventory_stays_locked(self):
        client, pos = self._ambiguous_position()
        client._fill(client.order_calls[0])
        client.extra += Decimal('0.00001')
        client.total += Decimal('0.00001')
        result = partial_spot_long.reconcile_pending_partial_long_spot(client, pos)
        self.assertEqual(result['status'], 'BALANCE_OR_ORDER_MISMATCH')
        self.assertTrue(pos['recovery_pending'])
        self.assertEqual(pos['quantity'], 0.00016)
        self.assertEqual(len(client.order_calls), 1)
        self.assertFalse(client.oco_calls)

    def test_p1_p2_managed_half_payload_is_fixed_decimal(self):
        client, pos = OfflinePartialClient(), _position()
        result = partial_spot_long.attempt_partial_long_spot(client, pos, 100000)
        self.assertTrue(result['confirmed_execution'])
        self.assertEqual(client.order_calls[0]['quantity'], '0.00008')
        self.assertNotIn('e', client.order_calls[0]['quantity'].lower())

    def test_p3_small_step_formats_without_exponent(self):
        self.assertEqual(format_decimal_quantity(Decimal('1E-8')), '0.00000001')

    def test_p4_below_min_qty_does_not_cancel_or_post(self):
        client, pos = OfflinePartialClient('0.00002', '0'), _position(0.00002)
        client.filters['min_qty'] = client.filters['market_min_qty'] = '0.00002'
        result = partial_spot_long.attempt_partial_long_spot(client, pos, 100000)
        self.assertEqual(result['status'], 'PARTIAL_NOT_OPERABLE')
        self.assertFalse(client.cancel_calls)
        self.assertFalse(client.order_calls)

    def test_p5_below_min_notional_does_not_cancel_or_post(self):
        client, pos = OfflinePartialClient(), _position()
        client.filters['min_notional'] = '9'
        result = partial_spot_long.attempt_partial_long_spot(client, pos, 100000)
        self.assertEqual(result['status'], 'PARTIAL_NOT_OPERABLE')
        self.assertFalse(client.cancel_calls)
        self.assertFalse(client.order_calls)

    def test_p6_prechecks_complete_before_cancel(self):
        client, pos = OfflinePartialClient(), _position()
        client.filters['step_size'] = '0'
        result = partial_spot_long.attempt_partial_long_spot(client, pos, 100000)
        self.assertEqual(result['status'], 'PREFLIGHT_FAILED')
        self.assertFalse(client.cancel_calls)

    def test_p7_success_updates_only_managed_residual(self):
        client, pos = OfflinePartialClient(), _position()
        partial_spot_long.attempt_partial_long_spot(client, pos, 100000)
        self.assertEqual(pos['quantity'], 0.00008)
        self.assertEqual(pos['oco_order_list_id'], '900')

    def test_p8_p9_deterministic_sell_failure_preserves_position(self):
        client, pos = OfflinePartialClient(), _position()
        client.sell_mode = 'http_error'
        result = partial_spot_long.attempt_partial_long_spot(client, pos, 100000)
        self.assertFalse(result['confirmed_execution'])
        self.assertEqual(pos['quantity'], 0.00016)
        self.assertFalse(pos['partial_taken'])
        self.assertNotIn('partial_pnl', pos)

    def test_p9_lifecycle_failure_does_not_write_pnl_or_analytics(self):
        pos = _position()
        state = {'total_pnl_usdt': 1.25, 'daily_pnl_usdt': -0.5}
        analytics = Mock()
        client = Mock()
        client.get_spot_price.return_value = 100000
        with patch.object(partial_spot_long, 'attempt_partial_long_spot', return_value={
            'status': 'SELL_FAILED_PROTECTED', 'confirmed_execution': False,
        }):
            position_lifecycle.check_partial_long(pos, state, client, Mock(), analytics, Mock())
        self.assertEqual(state, {'total_pnl_usdt': 1.25, 'daily_pnl_usdt': -0.5})
        self.assertFalse(pos['partial_taken'])
        analytics.log_trade_close.assert_not_called()

    def test_p10_failed_sell_restores_only_managed_quantity(self):
        client, pos = OfflinePartialClient(), _position()
        client.sell_mode = 'http_error'
        result = partial_spot_long.attempt_partial_long_spot(client, pos, 100000)
        self.assertEqual(result['status'], 'SELL_FAILED_PROTECTED')
        self.assertEqual(client.oco_calls[-1]['quantity'], '0.00016')

    def test_p11_p12_extra_inventory_is_excluded(self):
        client, pos = OfflinePartialClient('0.00016', '0.00002783'), _position()
        result = partial_spot_long.attempt_partial_long_spot(client, pos, 100000)
        self.assertEqual(result['excess_inventory'], 0.00002783)
        self.assertEqual(client.order_calls[0]['quantity'], '0.00008')
        self.assertEqual(client.oco_calls[-1]['quantity'], '0.00008')
        self.assertEqual(client.total, Decimal('0.00010783'))

    def test_p13_timeout_requeries_order(self):
        client, pos = OfflinePartialClient(), _position()
        client.sell_mode = 'timeout_executed'
        partial_spot_long.attempt_partial_long_spot(client, pos, 100000)
        self.assertEqual(len(client.lookup_calls), 1)

    def test_p14_timeout_with_execution_never_duplicates_sell(self):
        client, pos = OfflinePartialClient(), _position()
        client.sell_mode = 'timeout_executed'
        result = partial_spot_long.attempt_partial_long_spot(client, pos, 100000)
        self.assertTrue(result['confirmed_execution'])
        self.assertEqual(len(client.order_calls), 1)

    def test_p15_timeout_without_evidence_fails_closed(self):
        client, pos = OfflinePartialClient(), _position()
        client.sell_mode = 'timeout_unknown'
        result = partial_spot_long.attempt_partial_long_spot(client, pos, 100000)
        self.assertEqual(result['status'], 'AMBIGUOUS_SELL')
        self.assertTrue(pos['recovery_pending'])
        self.assertEqual(len(client.order_calls), 1)
        self.assertFalse(client.oco_calls)
        repeated = partial_spot_long.attempt_partial_long_spot(client, pos, 100000)
        self.assertEqual(repeated['status'], 'RECOVERY_PENDING_REQUIRES_RECONCILIATION')
        self.assertEqual(len(client.order_calls), 1)

    def test_p16_partial_execution_uses_exact_normalized_remaining(self):
        client, pos = OfflinePartialClient(), _position()
        client.executed = Decimal('0.00003')
        result = partial_spot_long.attempt_partial_long_spot(client, pos, 100000)
        self.assertEqual(result['remaining_quantity'], 0.00013)
        self.assertEqual(client.oco_calls[-1]['quantity'], '0.00013')

    def test_p17_dust_residual_uses_existing_handler(self):
        client, pos = OfflinePartialClient(), _position()
        normalizer = partial_spot_long._normalize
        oco_checks = 0

        def residual_becomes_dust(quantity, filters, price, market=False):
            nonlocal oco_checks
            if not market:
                oco_checks += 1
                if oco_checks == 2:
                    return Decimal('0'), {'reason': 'below_min_notional'}
            return normalizer(quantity, filters, price, market=market)

        with patch.object(partial_spot_long, '_normalize', side_effect=residual_becomes_dust), \
             patch.object(partial_spot_long.residuals, 'handle_unprotectable_spot_residual', return_value=True) as handler:
            result = partial_spot_long.attempt_partial_long_spot(client, pos, 100000)
        self.assertTrue(result['confirmed_execution'])
        self.assertTrue(result['residual_recorded'])
        self.assertEqual(handler.call_args.args[2], 0.00008)
        self.assertFalse(client.oco_calls)

    def test_live_partially_filled_status_does_not_finalize(self):
        client, pos = OfflinePartialClient(), _position()
        original = client.get_spot_order

        def not_terminal(params):
            order = original(params)
            order['status'] = 'PARTIALLY_FILLED'
            return order

        client.get_spot_order = not_terminal
        result = partial_spot_long.attempt_partial_long_spot(client, pos, 100000)
        self.assertEqual(result['status'], 'AMBIGUOUS_SELL')
        self.assertTrue(pos['recovery_pending'])
        self.assertFalse(client.oco_calls)

    def test_p18_recovery_oco_failure_is_observable_and_state_remains(self):
        client, pos = OfflinePartialClient(), _position()
        client.oco_error = 'offline OCO rejection'
        result = partial_spot_long.attempt_partial_long_spot(client, pos, 100000)
        self.assertEqual(result['status'], 'PARTIAL_EXECUTED_UNPROTECTED')
        self.assertEqual(pos['quantity'], 0.00008)
        self.assertTrue(pos['recovery_pending'])
        self.assertIn('offline OCO rejection', pos['protection_warning'])
        repeated = partial_spot_long.attempt_partial_long_spot(client, pos, 100000)
        self.assertEqual(repeated['status'], 'RECOVERY_PENDING_REQUIRES_RECONCILIATION')
        self.assertEqual(len(client.order_calls), 1)

    def test_p19_invalid_oco_snapshot_does_not_cancel(self):
        client, pos = OfflinePartialClient(), _position()
        pos['oco_order_ids'] = ['999']
        result = partial_spot_long.attempt_partial_long_spot(client, pos, 100000)
        self.assertEqual(result['status'], 'INVALID_OCO_SNAPSHOT')
        self.assertFalse(client.cancel_calls)

    def test_p20_inconsistent_symbol_side_or_quantity_blocks(self):
        client, pos = OfflinePartialClient(), _position()
        client.managed = Decimal('0.00015')
        result = partial_spot_long.attempt_partial_long_spot(client, pos, 100000)
        self.assertEqual(result['status'], 'INVALID_OCO_SNAPSHOT')
        self.assertFalse(client.cancel_calls)

    def test_p21_uses_only_injected_offline_client(self):
        client, pos = OfflinePartialClient(), _position()
        partial_spot_long.attempt_partial_long_spot(client, pos, 100000)
        self.assertEqual(len(client.order_calls), 1)

    def test_p25_scientific_input_is_serialized_without_scientific_output(self):
        for value in ('8e-05', Decimal('8E-5'), 0.00008):
            text = format_decimal_quantity(value)
            self.assertEqual(text, '0.00008')
            self.assertNotIn('e', text.lower())


if __name__ == '__main__':
    unittest.main()
