#!/usr/bin/env python3
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import Mock, patch

os.environ.setdefault('BINANCE_API_KEY', 'test')
os.environ.setdefault('BINANCE_API_SECRET', 'test')
sys.path.insert(0, os.path.dirname(__file__))

import config
import market
import preventive_futures_close
from orchestration import cycle_runner
from testing.fake_binance_client import FakeBinanceClient
from testing.fake_exchange_state import FakeExchangeState


class ScriptedClient:
    def __init__(self, positions, order=None, lookup=None, open_orders=None,
                 cleanup_errors=None, events=None):
        self.positions = list(positions)
        self.order = order
        self.lookup = lookup
        self.open_orders = list(open_orders or [])
        self.cleanup_errors = {str(item) for item in (cleanup_errors or set())}
        self.events = events if events is not None else []
        self.calls = []

    def _remember(self, operation, payload=None):
        self.calls.append((operation, payload))
        self.events.append(operation)

    def futures_position_risk(self, params=None):
        self._remember('positionRisk', params)
        item = self.positions.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def get_futures_filters(self, symbol):
        self._remember('filters', symbol)
        return {'step_size': 0.01, 'min_qty': 0.01}

    def create_futures_order(self, payload):
        self._remember('POST', dict(payload))
        if isinstance(self.order, BaseException):
            raise self.order
        return dict(self.order or {})

    def get_futures_order(self, payload):
        self._remember('GET_ORDER', dict(payload))
        if isinstance(self.lookup, BaseException):
            raise self.lookup
        return dict(self.lookup if self.lookup is not None else self.order or {})

    def futures_open_orders(self, params=None):
        self._remember('OPEN_ORDERS', params)
        return [dict(item) for item in self.open_orders]

    def cancel_futures_order(self, payload):
        self._remember('CANCEL', dict(payload))
        if str(payload.get('orderId')) in self.cleanup_errors:
            raise RuntimeError('cleanup failed')
        return {'status': 'CANCELED', **payload}


def position(symbol, amount):
    return [{'symbol': symbol, 'positionAmt': str(amount), 'entryPrice': '100', 'markPrice': '90'}]


def filled_order(symbol='TESTUSDT', quantity='1', price='90', order_id=700):
    return {
        'symbol': symbol,
        'orderId': order_id,
        'status': 'FILLED',
        'side': 'BUY',
        'type': 'MARKET',
        'executedQty': quantity,
        'avgPrice': price,
        'reduceOnly': True,
    }


def short_pos(symbol='TESTUSDT', quantity=1):
    return {
        'id': f'short_{symbol}_1',
        'direction': 'short',
        'symbol': symbol,
        'entry_price': 100.0,
        'quantity': quantity,
        'sl': 110.0,
        'tp': 80.0,
        'tp_order_id': 11,
        'sl_order_id': 12,
    }


class PreventiveFuturesCloseTests(unittest.TestCase):
    def setUp(self):
        self.timeline = patch('decision_timeline.record_event').start()
        self.addCleanup(patch.stopall)

    def test_normal_short_uses_fake_exchange_and_exact_reduce_only_payload(self):
        state = FakeExchangeState()
        state.set_price('TESTUSDT', 90)
        state.futures_positions['TESTUSDT'] = {
            'positionAmt': Decimal('-1'),
            'entryPrice': Decimal('100'),
            'leverage': 2,
        }
        state.orders[11] = {'symbol': 'TESTUSDT', 'orderId': 11, 'status': 'NEW',
                            'reduceOnly': True, 'type': 'LIMIT'}
        state.orders[12] = {'symbol': 'TESTUSDT', 'orderId': 12, 'status': 'NEW',
                            'reduceOnly': True, 'type': 'STOP_MARKET'}
        client = FakeBinanceClient(state)

        result = preventive_futures_close.attempt_preventive_short_close(client, short_pos())

        self.assertEqual('CONFIRMED_PREVENTIVE_CLOSE', result['status'])
        posts = [call for call in client.calls if call['operation'] == 'futures_signed:POST:/fapi/v1/order']
        self.assertEqual(1, len(posts))
        self.assertEqual({
            'symbol': 'TESTUSDT', 'side': 'BUY', 'type': 'MARKET',
            'quantity': '1.0', 'reduceOnly': 'true',
        }, posts[0]['payload']['params'])
        self.assertNotIn('positionSide', posts[0]['payload']['params'])
        operations = [call['operation'] for call in client.calls]
        post_index = operations.index('futures_signed:POST:/fapi/v1/order')
        position_indexes = [i for i, item in enumerate(operations) if item == 'futures_signed:GET:/fapi/v2/positionRisk']
        cancel_indexes = [i for i, item in enumerate(operations) if item == 'futures_signed:DELETE:/fapi/v1/order']
        self.assertLess(position_indexes[0], post_index)
        self.assertLess(post_index, position_indexes[1])
        self.assertTrue(cancel_indexes)
        self.assertLess(position_indexes[1], min(cancel_indexes))
        self.assertNotIn('get_futures_price', operations)

    def test_confirmed_close_uses_real_fill_then_logs_and_removes(self):
        events = []
        client = ScriptedClient(
            [position('TESTUSDT', -1), position('TESTUSDT', 0)],
            order=filled_order(),
            events=events,
        )
        safe_log = Mock(side_effect=lambda *args: events.append('TRADE_CLOSE'))
        runner = self._runner(client, safe_log)
        state = {'positions': [short_pos()], 'total_pnl_usdt': 2.0, 'daily_pnl_usdt': 1.0}
        pos = state['positions'][0]
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, 'trades.log')
            open(log_path, 'w', encoding='utf-8').close()
            with patch.object(config, 'TRADES_LOG', log_path):
                result, defer = runner._handle_preventive_short(state, state['positions'], pos)

        self.assertFalse(defer)
        self.assertEqual([], state['positions'])
        expected_pnl = (100 - 90) * 1 * (1 - config.FUTURES_FEE_RATE * 2)
        self.assertAlmostEqual(expected_pnl, result['pnl'])
        self.assertAlmostEqual(2.0 + expected_pnl, state['total_pnl_usdt'])
        safe_log.assert_called_once_with(pos, 90.0, 'PREVENTIVE_BTC_MOMENTUM', expected_pnl)
        self.assertLess(events.index('positionRisk', events.index('POST') + 1), events.index('TRADE_CLOSE'))

    def test_local_short_already_flat_sends_no_market_and_defers_to_existing_lifecycle(self):
        client = ScriptedClient([position('TESTUSDT', 0)])
        result = preventive_futures_close.attempt_preventive_short_close(client, short_pos())
        self.assertEqual('ALREADY_FLAT', result['status'])
        self.assertNotIn('POST', [item[0] for item in client.calls])
        runner = self._runner(client)
        state = {'positions': [short_pos()], 'total_pnl_usdt': 0, 'daily_pnl_usdt': 0}
        with patch('preventive_futures_close.attempt_preventive_short_close', return_value=result):
            _, defer = runner._handle_preventive_short(state, state['positions'], state['positions'][0])
        self.assertFalse(defer)
        self.assertEqual(1, len(state['positions']))

    def test_positive_exchange_position_fails_closed_without_buy(self):
        client = ScriptedClient([position('TESTUSDT', 1)])
        result = preventive_futures_close.attempt_preventive_short_close(client, short_pos())
        self.assertEqual('DIRECTION_MISMATCH', result['status'])
        self.assertNotIn('POST', [item[0] for item in client.calls])
        self.assertFalse(result['confirmed_close'])

    def test_post_error_and_same_short_keeps_position_and_protection(self):
        client = ScriptedClient(
            [position('TESTUSDT', -1), position('TESTUSDT', -1)],
            order=RuntimeError('rejected'),
        )
        result = preventive_futures_close.attempt_preventive_short_close(client, short_pos())
        self.assertEqual('POSITION_STILL_OPEN', result['status'])
        self.assertNotIn('CANCEL', [item[0] for item in client.calls])
        self.assertFalse(result['confirmed_close'])

    def test_post_error_then_flat_is_unattributed_race(self):
        client = ScriptedClient(
            [position('TESTUSDT', -1), position('TESTUSDT', 0)],
            order=RuntimeError('reduce-only rejected'),
        )
        result = preventive_futures_close.attempt_preventive_short_close(client, short_pos())
        self.assertEqual('FLAT_UNATTRIBUTED', result['status'])
        self.assertIsNone(result['fill_price'])
        self.assertNotIn('pnl', result)
        self.assertFalse(result['confirmed_close'])

    def test_timeout_rechecks_position_and_preserves_open_state(self):
        client = ScriptedClient(
            [position('TESTUSDT', -1), position('TESTUSDT', -1)],
            order=TimeoutError('unknown result'),
        )
        result = preventive_futures_close.attempt_preventive_short_close(client, short_pos())
        self.assertEqual('POSITION_STILL_OPEN', result['status'])
        self.assertEqual(2, [item[0] for item in client.calls].count('positionRisk'))
        self.assertFalse(result['confirmed_close'])

    def test_unavailable_post_position_fails_closed(self):
        client = ScriptedClient(
            [position('TESTUSDT', -1), TimeoutError('position unavailable')],
            order=filled_order(),
        )
        result = preventive_futures_close.attempt_preventive_short_close(client, short_pos())
        self.assertEqual('POST_POSITION_UNKNOWN', result['status'])
        self.assertFalse(result['confirmed_close'])
        self.assertNotIn('CANCEL', [item[0] for item in client.calls])

    def test_partial_fill_keeps_exchange_residual_quantity(self):
        order = filled_order(quantity='0.5')
        order['status'] = 'PARTIALLY_FILLED'
        client = ScriptedClient(
            [position('TESTUSDT', -1), position('TESTUSDT', -0.5)],
            order=order,
        )
        result = preventive_futures_close.attempt_preventive_short_close(client, short_pos())
        self.assertEqual('RESIDUAL_POSITION', result['status'])
        self.assertEqual(0.5, result['remaining_quantity'])
        runner = self._runner(client)
        state = {'positions': [short_pos()], 'total_pnl_usdt': 0, 'daily_pnl_usdt': 0}
        with patch('preventive_futures_close.attempt_preventive_short_close', return_value=result):
            _, defer = runner._handle_preventive_short(state, state['positions'], state['positions'][0])
        self.assertTrue(defer)
        self.assertEqual(0.5, state['positions'][0]['quantity'])

    def test_protections_are_not_cancelled_before_market_attempt(self):
        client = ScriptedClient(
            [position('TESTUSDT', -1), position('TESTUSDT', 0)],
            order=filled_order(),
            open_orders=[{'symbol': 'TESTUSDT', 'orderId': 11, 'reduceOnly': True}],
        )
        preventive_futures_close.attempt_preventive_short_close(client, short_pos())
        operations = [item[0] for item in client.calls]
        self.assertLess(operations.index('POST'), operations.index('CANCEL'))
        self.assertLess(operations.index('positionRisk', operations.index('POST') + 1), operations.index('CANCEL'))

    def test_confirmed_flat_cleans_tp_and_sl_once_each(self):
        client = ScriptedClient(
            [position('TESTUSDT', -1), position('TESTUSDT', 0)],
            order=filled_order(),
            open_orders=[
                {'symbol': 'TESTUSDT', 'orderId': 11, 'reduceOnly': True},
                {'symbol': 'TESTUSDT', 'orderId': 12, 'reduceOnly': True},
            ],
        )
        result = preventive_futures_close.attempt_preventive_short_close(client, short_pos())
        cancelled = [payload['orderId'] for operation, payload in client.calls if operation == 'CANCEL']
        self.assertEqual([11, 12], cancelled)
        self.assertEqual([11, 12], result['cancelled_order_ids'])

    def test_cleanup_failure_remains_observable_without_reviving_exposure(self):
        client = ScriptedClient(
            [position('TESTUSDT', -1), position('TESTUSDT', 0)],
            order=filled_order(),
            cleanup_errors={12},
        )
        result = preventive_futures_close.attempt_preventive_short_close(client, short_pos())
        self.assertTrue(result['confirmed_close'])
        self.assertEqual('cancel_protection', result['cleanup_errors'][0]['stage'])

    def test_filled_response_without_full_execution_is_not_attributed(self):
        order = filled_order(quantity='0.5')
        client = ScriptedClient(
            [position('TESTUSDT', -1), position('TESTUSDT', 0)],
            order=order,
        )
        result = preventive_futures_close.attempt_preventive_short_close(client, short_pos())
        self.assertEqual('FLAT_UNATTRIBUTED', result['status'])
        self.assertFalse(result['confirmed_close'])

    def test_missing_fill_price_never_falls_back_to_observed_price(self):
        order = filled_order()
        order['avgPrice'] = '0'
        client = ScriptedClient(
            [position('TESTUSDT', -1), position('TESTUSDT', 0)],
            order=order,
        )
        result = preventive_futures_close.attempt_preventive_short_close(client, short_pos())
        self.assertEqual('FLAT_UNATTRIBUTED', result['status'])
        self.assertNotIn('pnl', result)

    def test_one_short_failure_does_not_prevent_another_confirmation(self):
        failed = ScriptedClient(
            [position('AUSDT', -1), position('AUSDT', -1)],
            order=RuntimeError('rejected'),
        )
        confirmed = ScriptedClient(
            [position('BUSDT', -2), position('BUSDT', 0)],
            order=filled_order('BUSDT', '2', '95', 701),
        )
        first = preventive_futures_close.attempt_preventive_short_close(failed, short_pos('AUSDT'))
        second = preventive_futures_close.attempt_preventive_short_close(confirmed, short_pos('BUSDT', 2))
        self.assertEqual('POSITION_STILL_OPEN', first['status'])
        self.assertEqual('CONFIRMED_PREVENTIVE_CLOSE', second['status'])

    def test_no_short_positions_produce_no_exchange_calls(self):
        client = ScriptedClient([])
        candidates = [item for item in [] if item.get('direction') == 'short']
        for item in candidates:
            preventive_futures_close.attempt_preventive_short_close(client, item)
        self.assertEqual([], client.calls)

    def test_long_is_explicitly_out_of_scope_and_sends_no_exchange_call(self):
        client = ScriptedClient([])
        pos = short_pos()
        pos['direction'] = 'long'
        result = preventive_futures_close.attempt_preventive_short_close(client, pos)
        self.assertEqual('NOT_A_SHORT', result['status'])
        self.assertEqual([], client.calls)

    def test_btc_momentum_threshold_contract_is_unchanged(self):
        self.assertEqual((False, False, None), market.check_btc_momentum_close({'change_4h': 3.99}))
        close_shorts, close_longs, reason = market.check_btc_momentum_close({'change_4h': 4.0})
        self.assertTrue(close_shorts)
        self.assertFalse(close_longs)
        self.assertIn('en 4h', reason)

    def test_unconfirmed_handler_preserves_state_pnl_and_trade_log(self):
        result = {'status': 'DIRECTION_MISMATCH', 'symbol': 'TESTUSDT', 'confirmed_close': False}
        client = ScriptedClient([])
        safe_log = Mock()
        runner = self._runner(client, safe_log)
        pos = short_pos()
        state = {'positions': [pos], 'total_pnl_usdt': 3.0, 'daily_pnl_usdt': 1.0}
        with patch('preventive_futures_close.attempt_preventive_short_close', return_value=result):
            returned, defer = runner._handle_preventive_short(state, state['positions'], pos)
        self.assertIs(result, returned)
        self.assertTrue(defer)
        self.assertEqual([pos], state['positions'])
        self.assertEqual(3.0, state['total_pnl_usdt'])
        self.assertEqual(1.0, state['daily_pnl_usdt'])
        safe_log.assert_not_called()

    def _runner(self, client, safe_log=None):
        return cycle_runner.CycleRunner(
            out_fn=Mock(), analytics=Mock(), binance=client,
            safe_log_open_fn=Mock(), safe_log_close_fn=safe_log or Mock(),
            safe_log_decision_snapshot_fn=Mock(), safe_persist_bot_state_fn=Mock(),
            audit_orphans_fn=Mock(), maybe_clean_dust_fn=Mock(),
            check_partial_long_fn=Mock(), check_partial_short_fn=Mock(), handle_close_fn=Mock(),
        )


if __name__ == '__main__':
    unittest.main()
