#!/usr/bin/env python3
import io
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

    def test_p16_partial_execution_uses_exact_normalized_remaining(self):
        client, pos = OfflinePartialClient(), _position()
        client.executed = Decimal('0.00003')
        result = partial_spot_long.attempt_partial_long_spot(client, pos, 100000)
        self.assertEqual(result['remaining_quantity'], 0.00013)
        self.assertEqual(client.oco_calls[-1]['quantity'], '0.00013')

    def test_p18_recovery_oco_failure_is_observable_and_state_remains(self):
        client, pos = OfflinePartialClient(), _position()
        client.oco_error = 'offline OCO rejection'
        result = partial_spot_long.attempt_partial_long_spot(client, pos, 100000)
        self.assertEqual(result['status'], 'PARTIAL_EXECUTED_UNPROTECTED')
        self.assertEqual(pos['quantity'], 0.00008)
        self.assertTrue(pos['recovery_pending'])
        self.assertIn('offline OCO rejection', pos['protection_warning'])

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
