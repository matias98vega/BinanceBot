#!/usr/bin/env python3
import json
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
import preventive_futures_close
import preventive_spot_close
from orchestration import cycle_runner
from testing.fake_binance_client import FakeBinanceClient
from testing.fake_exchange_state import FakeExchangeState


SYMBOL = 'TESTUSDT'


def long_pos(quantity=1, **changes):
    pos = {
        'id': 'long_TESTUSDT_1',
        'direction': 'long',
        'symbol': SYMBOL,
        'entry_price': 100.0,
        'quantity': quantity,
        'sl': 80.0,
        'tp': 120.0,
        'entry_time': 1,
        'oco_order_list_id': '',
        'oco_order_ids': [],
    }
    pos.update(changes)
    return pos


class TimeoutAfterExecutionClient(FakeBinanceClient):
    def create_spot_order(self, params):
        super().create_spot_order(params)
        raise TimeoutError('response lost after exchange execution')


class PartialFillClient(FakeBinanceClient):
    def __init__(self, state, executed_quantity):
        super().__init__(state)
        self.executed_quantity = Decimal(str(executed_quantity))

    def create_spot_order(self, params):
        requested = Decimal(str(params['quantity']))
        partial = dict(params)
        partial['quantity'] = str(self.executed_quantity)
        order = super().create_spot_order(partial)
        order['origQty'] = str(requested)
        order['status'] = 'PARTIALLY_FILLED'
        self.state.orders[order['orderId']] = dict(order)
        return order


class PriceMovesBeforeFillClient(FakeBinanceClient):
    def __init__(self, state, fill_price):
        super().__init__(state)
        self.fill_price = fill_price

    def create_spot_order(self, params):
        self.state.set_price(params['symbol'], self.fill_price)
        return super().create_spot_order(params)


class FailPostBalanceClient(FakeBinanceClient):
    def __init__(self, state):
        super().__init__(state)
        self.account_reads = 0

    def get_spot_account(self):
        self.account_reads += 1
        if self.account_reads == 3:
            raise TimeoutError('post-sell balance unavailable')
        return super().get_spot_account()

    spot_account = get_spot_account


class PreventiveSpotCloseTests(unittest.TestCase):
    def setUp(self):
        self.timeline = patch('decision_timeline.record_event').start()
        self.addCleanup(patch.stopall)
        self.residual_events = []

    def record_residual(self, symbol, asset, quantity, price, filters, **kwargs):
        self.residual_events.append({
            'symbol': symbol, 'asset': asset, 'quantity': quantity,
            'price': price, 'reason': kwargs.get('reason'),
        })
        return True

    def fixture(self, *, local_quantity=1, exchange_quantity=None, protected_quantity=None,
                price=90, client_class=FakeBinanceClient, client_args=(), with_oco=True,
                step='0.001', min_qty='0.001', min_notional='5'):
        exchange_quantity = local_quantity if exchange_quantity is None else exchange_quantity
        protected_quantity = (
            min(Decimal(str(local_quantity)), Decimal(str(exchange_quantity)))
            if protected_quantity is None else protected_quantity
        )
        state = FakeExchangeState()
        state.set_price(SYMBOL, price)
        state.set_filters(
            SYMBOL,
            step_size=step,
            market_step_size=step,
            min_qty=min_qty,
            market_min_qty=min_qty,
            min_notional=min_notional,
        )
        state.set_balance('TEST', exchange_quantity)
        client = client_class(state, *client_args)
        pos = long_pos(local_quantity)
        if with_oco:
            oco = client.create_oco({
                'symbol': SYMBOL,
                'side': 'SELL',
                'quantity': str(protected_quantity),
                'price': '120',
                'stopPrice': '80',
                'stopLimitPrice': '79.9',
                'stopLimitTimeInForce': 'GTC',
            })
            pos['oco_order_list_id'] = str(oco['orderListId'])
            pos['oco_order_ids'] = [str(item['orderId']) for item in oco['orders']]
        client.calls.clear()
        return client, pos

    def close(self, client, pos):
        return preventive_spot_close.attempt_preventive_long_spot_close(
            client, pos, residual_handler=self.record_residual,
        )

    @staticmethod
    def operations(client, operation):
        return [call for call in client.calls if call['operation'] == operation]

    def test_l1_open_long_oco_sell_success_and_flat_confirmation(self):
        client, pos = self.fixture()

        result = self.close(client, pos)

        self.assertEqual('CONFIRMED_PREVENTIVE_CLOSE', result['status'])
        self.assertTrue(result['confirmed_close'])
        self.assertEqual(0, client.state.balance('TEST')['free'])
        self.assertEqual(0, client.state.balance('TEST')['locked'])
        posts = self.operations(client, 'spot_signed:POST:/api/v3/order')
        self.assertEqual(1, len(posts))
        self.assertEqual('SELL', posts[0]['payload']['params']['side'])
        self.assertEqual('MARKET', posts[0]['payload']['params']['type'])
        self.assertNotIn('reduceOnly', posts[0]['payload']['params'])

    def test_l2_sell_failure_keeps_local_and_no_close_contract(self):
        client, pos = self.fixture()
        client.state.queue_error('spot_signed:POST:/api/v3/order', RuntimeError('SELL failed'))

        result = self.close(client, pos)

        self.assertFalse(result['confirmed_close'])
        self.assertEqual('MARKET_FAILED_PROTECTED', result['status'])
        self.assertEqual(1.0, pos['quantity'])

    def test_l3_timeout_after_execution_requeries_and_does_not_double_sell(self):
        client, pos = self.fixture(client_class=TimeoutAfterExecutionClient)

        first = self.close(client, pos)
        second = self.close(client, pos)

        self.assertTrue(first['confirmed_close'])
        self.assertEqual('ALREADY_FLAT_UNATTRIBUTED', second['status'])
        self.assertEqual(1, len(self.operations(client, 'spot_signed:POST:/api/v3/order')))

    def test_l4_timeout_while_position_remains_open_fails_closed(self):
        client, pos = self.fixture()
        client.state.queue_error('spot_signed:POST:/api/v3/order', TimeoutError('timeout before execution'))

        result = self.close(client, pos)

        self.assertEqual('MARKET_FAILED_PROTECTED', result['status'])
        self.assertFalse(result['confirmed_close'])
        self.assertEqual(Decimal('1'), client.state.balance('TEST')['locked'])

    def test_l5_already_flat_dust_never_sends_second_sell(self):
        client, pos = self.fixture(exchange_quantity='0.001', with_oco=False)

        result = self.close(client, pos)

        self.assertEqual('ALREADY_FLAT_UNATTRIBUTED', result['status'])
        self.assertEqual([], self.operations(client, 'spot_signed:POST:/api/v3/order'))

    def test_l6_unexpected_direction_is_blocked_before_exchange_calls(self):
        client, pos = self.fixture()
        pos['direction'] = 'short'

        result = self.close(client, pos)

        self.assertEqual('INVALID_MANAGED_SPOT_LONG', result['status'])
        self.assertEqual([], client.calls)

    def test_l7_local_quantity_above_exchange_uses_exchange_quantity(self):
        client, pos = self.fixture(local_quantity=1, exchange_quantity='0.6', protected_quantity='0.6')

        result = self.close(client, pos)

        self.assertTrue(result['confirmed_close'])
        payload = self.operations(client, 'spot_signed:POST:/api/v3/order')[0]['payload']['params']
        self.assertEqual('0.6', payload['quantity'])

    def test_l8_local_below_exchange_by_dust_sells_only_attributable_quantity(self):
        client, pos = self.fixture(local_quantity=1, exchange_quantity='1.004', protected_quantity='1')

        result = self.close(client, pos)

        self.assertTrue(result['confirmed_close'])
        payload = self.operations(client, 'spot_signed:POST:/api/v3/order')[0]['payload']['params']
        self.assertEqual('1', payload['quantity'])
        self.assertEqual(Decimal('0.004'), client.state.balance('TEST')['free'])

    def test_l9_canonical_oco_id_is_queried_and_cancelled(self):
        client, pos = self.fixture()
        canonical = int(pos['oco_order_list_id'])

        self.close(client, pos)

        cancel = self.operations(client, 'spot_signed:DELETE:/api/v3/orderList')
        self.assertEqual(canonical, cancel[0]['payload']['params']['orderListId'])

    def test_l10_legacy_oco_id_is_not_treated_as_canonical(self):
        client, pos = self.fixture(with_oco=False)
        pos['oco_id'] = '123'

        result = self.close(client, pos)

        self.assertEqual('LEGACY_OCO_IDENTIFIER', result['status'])
        self.assertEqual([], client.calls)

    def test_l11_cancel_oco_failure_prevents_sell(self):
        client, pos = self.fixture()
        client.state.queue_error('spot_signed:DELETE:/api/v3/orderList', RuntimeError('cancel failed'))

        result = self.close(client, pos)

        self.assertEqual('CANCEL_OCO_FAILED', result['status'])
        self.assertEqual([], self.operations(client, 'spot_signed:POST:/api/v3/order'))

    def test_l12_cancelled_oco_and_sell_failure_keeps_local_and_restores(self):
        client, pos = self.fixture()
        client.state.queue_error('spot_signed:POST:/api/v3/order', RuntimeError('sell failed'))

        result = self.close(client, pos)

        self.assertEqual('MARKET_FAILED_PROTECTED', result['status'])
        self.assertIn(pos, [pos])
        self.assertTrue(pos['oco_order_list_id'])

    def test_l13_restoration_success_keeps_position_managed_and_protected(self):
        client, pos = self.fixture()
        original = pos['oco_order_list_id']
        client.state.queue_error('spot_signed:POST:/api/v3/order', RuntimeError('sell failed'))

        result = self.close(client, pos)

        self.assertTrue(result['restore']['restored'])
        self.assertNotEqual(original, pos['oco_order_list_id'])
        self.assertFalse(pos['recovery_pending'])

    def test_l14_restoration_failure_is_critical_and_local_remains(self):
        client, pos = self.fixture()
        client.state.queue_error('spot_signed:POST:/api/v3/order', RuntimeError('sell failed'))
        client.state.queue_error('spot_signed:POST:/api/v3/order/oco', RuntimeError('restore failed'))

        result = self.close(client, pos)

        self.assertEqual('MARKET_FAILED_UNPROTECTED', result['status'])
        self.assertTrue(pos['recovery_pending'])
        self.assertEqual('', pos['oco_order_list_id'])

    def test_l15_partial_fill_keeps_residual_instead_of_false_total_close(self):
        client, pos = self.fixture(client_class=PartialFillClient, client_args=('0.4',))

        result = self.close(client, pos)

        self.assertEqual('PARTIAL_RESIDUAL_PROTECTED', result['status'])
        self.assertFalse(result['confirmed_close'])
        self.assertEqual(0.6, pos['quantity'])

    def test_l16_operable_residual_gets_canonical_oco(self):
        client, pos = self.fixture(client_class=PartialFillClient, client_args=('0.4',))

        self.close(client, pos)

        self.assertTrue(pos['oco_order_list_id'])
        self.assertEqual(2, len(pos['oco_order_ids']))

    def test_l17_non_operable_dust_is_recorded_and_close_can_finish(self):
        client, pos = self.fixture(client_class=PartialFillClient, client_args=('0.995',))

        result = self.close(client, pos)

        self.assertEqual('CONFIRMED_PREVENTIVE_CLOSE_WITH_DUST', result['status'])
        self.assertTrue(result['confirmed_close'])
        self.assertTrue(self.residual_events)

    def test_dust_record_failure_blocks_logical_close(self):
        client, pos = self.fixture(client_class=PartialFillClient, client_args=('0.995',))

        result = preventive_spot_close.attempt_preventive_long_spot_close(
            client, pos, residual_handler=lambda *args, **kwargs: False,
        )

        self.assertEqual('DUST_RECORD_FAILED', result['status'])
        self.assertFalse(result['confirmed_close'])
        self.assertTrue(pos['recovery_pending'])

    def test_locked_balance_without_canonical_oco_fails_closed(self):
        client, pos = self.fixture(with_oco=False)
        client.state.set_balance('TEST', 0, 1)

        result = self.close(client, pos)

        self.assertEqual('LOCKED_BALANCE_WITHOUT_CANONICAL_OCO', result['status'])
        self.assertEqual([], self.operations(client, 'spot_signed:POST:/api/v3/order'))

    def test_decimal_evidence_is_json_serializable(self):
        client, pos = self.fixture(exchange_quantity='0.001', with_oco=False)

        self.close(client, pos)

        details = self.timeline.call_args.kwargs['details']
        json.dumps(details)

    def test_l18_pnl_uses_real_fill_price_and_executed_quantity(self):
        client, pos = self.fixture(client_class=PriceMovesBeforeFillClient, client_args=(80,))

        result = self.close(client, pos)

        expected = (80 - 100) * result['executed_quantity'] * (1 - config.BNB_FEE_RATE * 2)
        self.assertEqual(80.0, result['fill_price'])
        self.assertAlmostEqual(expected, result['pnl'])

    def test_l19_unconfirmed_cycle_result_never_logs_trade_close(self):
        runner, safe_close = self.runner()
        pos = long_pos()
        state = {'positions': [pos], 'total_pnl_usdt': 0, 'daily_pnl_usdt': 0}
        with patch('preventive_spot_close.attempt_preventive_long_spot_close', return_value={
            'status': 'MARKET_FAILED_PROTECTED', 'confirmed_close': False,
        }):
            runner._handle_preventive_long_spot(state, state['positions'], pos)
        safe_close.assert_not_called()

    def test_l20_unconfirmed_cycle_result_never_removes_local_position(self):
        runner, _ = self.runner()
        pos = long_pos()
        state = {'positions': [pos], 'total_pnl_usdt': 0, 'daily_pnl_usdt': 0}
        with patch('preventive_spot_close.attempt_preventive_long_spot_close', return_value={
            'status': 'AMBIGUOUS_CLOSE', 'confirmed_close': False,
        }):
            _, defer = runner._handle_preventive_long_spot(state, state['positions'], pos)
        self.assertTrue(defer)
        self.assertEqual([pos], state['positions'])

    def test_l21_initial_get_failure_never_cancels_or_sells(self):
        client, pos = self.fixture()
        client.state.queue_error('get_spot_account', TimeoutError('account unavailable'))

        result = self.close(client, pos)

        self.assertEqual('PREFLIGHT_EXCHANGE_UNKNOWN', result['status'])
        self.assertEqual([], self.operations(client, 'spot_signed:DELETE:/api/v3/orderList'))
        self.assertEqual([], self.operations(client, 'spot_signed:POST:/api/v3/order'))

    def test_l22_post_sell_balance_failure_remains_ambiguous(self):
        client, pos = self.fixture(client_class=FailPostBalanceClient)

        result = self.close(client, pos)

        self.assertEqual('POST_BALANCE_UNKNOWN', result['status'])
        self.assertFalse(result['confirmed_close'])
        self.assertTrue(pos['recovery_pending'])

    def test_l23_client_order_id_is_deterministic_and_unique_to_trade(self):
        first = preventive_spot_close._client_order_id(long_pos(), SYMBOL)
        second = preventive_spot_close._client_order_id(long_pos(), SYMBOL)
        other = preventive_spot_close._client_order_id(long_pos(id='long_TESTUSDT_2'), SYMBOL)
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertLessEqual(len(first), 36)

    def test_l24_success_path_uses_only_zero_network_fake_client(self):
        client, pos = self.fixture()

        result = self.close(client, pos)

        self.assertTrue(result['confirmed_close'])
        self.assertIsInstance(client, FakeBinanceClient)
        self.assertTrue(all('http' not in call['operation'].lower() for call in client.calls))

    def test_l25_short_preventive_contract_remains_reduce_only_buy(self):
        state = FakeExchangeState()
        state.set_price(SYMBOL, 90)
        state.futures_positions[SYMBOL] = {
            'positionAmt': Decimal('-1'), 'entryPrice': Decimal('100'), 'leverage': 2,
        }
        client = FakeBinanceClient(state)
        pos = {
            'id': 'short_TESTUSDT_1', 'direction': 'short', 'symbol': SYMBOL,
            'entry_price': 100.0, 'quantity': 1.0, 'tp_order_id': '', 'sl_order_id': '',
        }

        result = preventive_futures_close.attempt_preventive_short_close(client, pos)

        self.assertTrue(result['confirmed_close'])
        payload = self.operations(client, 'futures_signed:POST:/fapi/v1/order')[0]['payload']['params']
        self.assertEqual('BUY', payload['side'])
        self.assertEqual('true', payload['reduceOnly'])

    def test_l26_preventive_telegram_event_key_is_unchanged(self):
        with patch.object(cycle_runner.utils, 'send_alert') as send, \
             patch.object(cycle_runner.utils, 'rearm_telegram_alert_event'):
            cycle_runner.sync_preventive_telegram_alert(False, True, 'fixture')
        send.assert_called_once_with(
            'fixture', event_key=cycle_runner.PREVENTIVE_BTC_FALL_CLOSE_LONGS_EVENT,
        )
        self.assertEqual('preventive_btc_fall:close_longs', cycle_runner.PREVENTIVE_BTC_FALL_CLOSE_LONGS_EVENT)

    def test_l27_cycle_runner_delegates_and_only_finalizes_confirmed_close(self):
        runner, safe_close = self.runner()
        pos = long_pos()
        state = {'positions': [pos], 'total_pnl_usdt': 0, 'daily_pnl_usdt': 0}
        result = {
            'status': 'CONFIRMED_PREVENTIVE_CLOSE', 'confirmed_close': True,
            'fill_price': 90.0, 'pnl': -9.985,
        }
        with patch('preventive_spot_close.attempt_preventive_long_spot_close', return_value=result) as helper:
            returned, defer = runner._handle_preventive_long_spot(state, state['positions'], pos)
        helper.assert_called_once_with(runner.binance, pos)
        safe_close.assert_called_once_with(pos, 90.0, 'PREVENTIVE_BTC_MOMENTUM', -9.985)
        self.assertEqual([], state['positions'])
        self.assertFalse(defer)
        self.assertIs(returned, result)

    def runner(self):
        safe_close = Mock()
        runner = cycle_runner.CycleRunner(
            out_fn=Mock(), analytics=Mock(), binance=Mock(),
            safe_log_open_fn=Mock(), safe_log_close_fn=safe_close,
            safe_log_decision_snapshot_fn=Mock(), safe_persist_bot_state_fn=Mock(),
            audit_orphans_fn=Mock(), maybe_clean_dust_fn=Mock(),
            check_partial_long_fn=Mock(), check_partial_short_fn=Mock(),
            handle_close_fn=Mock(),
        )
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = os.path.join(temp.name, 'trades.log')
        with open(path, 'w', encoding='utf-8'):
            pass
        patcher = patch.object(config, 'TRADES_LOG', path)
        patcher.start()
        self.addCleanup(patcher.stop)
        return runner, safe_close


if __name__ == '__main__':
    unittest.main()
