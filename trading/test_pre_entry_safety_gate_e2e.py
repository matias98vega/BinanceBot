#!/usr/bin/env python3
import copy
import os
import socket
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import longs
import bot_state
import pre_entry_gate_observability
import pre_entry_safety_gate as gate
import shorts
from testing import FakeBinanceClient, FakeExchangeState


def fake_client():
    state = FakeExchangeState()
    state.set_balance('USDT', 100)
    for symbol, price in (('BTCUSDT', 100), ('ETHUSDT', 20)):
        state.set_price(symbol, price)
        state.set_filters(symbol, tick_size='.01', step_size='.001', min_qty='.001', min_notional='5')
        state.set_filters(symbol, futures=True, tick_size='.01', step_size='.001', min_qty='.001', min_notional='5')
    return FakeBinanceClient(state)


def result(client, local=None, side='LONG', symbol='BTCUSDT', capacity=None, **kwargs):
    return gate.evaluate_pre_entry_safety(
        client=client, local_state=local or {'positions': []}, bot_state={}, side=side, symbol=symbol,
        context={'capacity': capacity or {'current': 0, 'operational_max': 2, 'new_entries_allowed': True}},
        now=1700000000, mode=gate.ENFORCE, **kwargs,
    )


class PreEntryGateEndToEndTests(unittest.TestCase):
    def test_safe_long_sends_one_buy_then_oco(self):
        fake = fake_client(); gate_result = result(fake)
        candidate = {'symbol': 'BTCUSDT', 'sl': 90, 'tp': 110, 'atr': 2, 'score': 8, 'reasons': []}
        with patch.object(longs, 'BINANCE', fake), patch.object(longs.config, 'DRY_RUN', False), \
             patch.object(longs.utils, 'get_spot_risk_pct', return_value=.1), \
             patch.object(longs.utils, 'get_spot_capital_per_position', return_value=10), \
             patch.object(longs.utils, 'validate_position_capacity', return_value=(True, '', 0, 2)), \
             patch.object(longs.capital_manager, 'validate_spot_order', return_value=(True, '', {})), \
             patch.object(longs.decision_timeline, 'record_signal_evaluated'), \
             patch.object(longs.decision_timeline, 'record_order_event'), \
             patch.object(longs.decision_timeline, 'record_protection_event'):
            position, _ = longs.open_long(candidate, {'positions': []}, max_longs=2, pre_entry_gate_result=gate_result)
        self.assertIsNotNone(position)
        writes = [call for call in fake.calls if call['operation'].startswith('spot_signed:POST')]
        self.assertEqual([item['operation'] for item in writes], ['spot_signed:POST:/api/v3/order', 'spot_signed:POST:/api/v3/order/oco'])

    def test_safe_short_sends_one_open_and_protection(self):
        fake = fake_client(); gate_result = result(fake, side='SHORT')
        candidate = {'symbol': 'BTCUSDT', 'sl': 110, 'tp': 90, 'atr': 2, 'score': 8, 'reasons': []}
        with patch.object(shorts, 'BINANCE', fake), patch.object(shorts.config, 'DRY_RUN', False), \
             patch.object(shorts.config, 'NATIVE_SL_ENABLED', False), patch.object(shorts.time, 'sleep'), \
             patch.object(shorts.capital_manager, 'get_limits', return_value={}), \
             patch.object(shorts.capital_manager, 'futures_usable_capital', return_value=100), \
             patch.object(shorts.utils, 'get_futures_notional_per_position', return_value=10), \
             patch.object(shorts.utils, 'validate_position_capacity', return_value=(True, '', 0, 2)), \
             patch.object(shorts.capital_manager, 'validate_futures_order', return_value=(True, '', {})), \
             patch.object(shorts.decision_timeline, 'record_signal_evaluated'), \
             patch.object(shorts.decision_timeline, 'record_order_event'), \
             patch.object(shorts.decision_timeline, 'record_protection_event'):
            position, _ = shorts.open_short(candidate, {'positions': []}, max_shorts=2, pre_entry_gate_result=gate_result)
        self.assertIsNotNone(position)
        orders = [call for call in fake.calls if call['operation'] == 'futures_signed:POST:/fapi/v1/order']
        self.assertEqual(len(orders), 2)
        self.assertNotIn('reduceOnly', orders[0]['payload']['params'])
        self.assertEqual(orders[1]['payload']['params']['reduceOnly'], 'true')

    def test_blocked_long_and_short_send_zero_writes(self):
        for side, module in (('LONG', longs), ('SHORT', shorts)):
            fake = fake_client()
            blocked = result(fake, side=side, capacity={'current': 2, 'operational_max': 2, 'new_entries_allowed': False})
            candidate = {'symbol': 'BTCUSDT', 'sl': 90, 'tp': 110, 'atr': 2, 'score': 8, 'reasons': []}
            with patch.object(module, 'BINANCE', fake), patch.object(module.config, 'DRY_RUN', False), \
                 patch.object(module.utils, 'validate_position_capacity', return_value=(True, '', 0, 2)), \
                 patch.object(module.decision_timeline, 'record_signal_evaluated'):
                if side == 'LONG':
                    with patch.object(longs.utils, 'get_spot_capital_per_position', return_value=10), \
                         patch.object(longs.capital_manager, 'validate_spot_order', return_value=(True, '', {})):
                        position, message = module.open_long(candidate, {'positions': []}, max_longs=2, pre_entry_gate_result=blocked)
                else:
                    with patch.object(shorts.capital_manager, 'get_limits', return_value={}), \
                         patch.object(shorts.capital_manager, 'futures_usable_capital', return_value=100), \
                         patch.object(shorts.utils, 'get_futures_notional_per_position', return_value=10), \
                         patch.object(shorts.capital_manager, 'validate_futures_order', return_value=(True, '', {})):
                        position, message = module.open_short(candidate, {'positions': []}, max_shorts=2, pre_entry_gate_result=blocked)
            self.assertIsNone(position); self.assertEqual(message, 'PRE_ENTRY_GATE_CAPACITY')
            self.assertFalse(any(':POST:' in call['operation'] for call in fake.calls))

    def test_scenarios_mismatch_external_close_missing_oco_orphan_timeout_and_multiple(self):
        fake = fake_client(); fake.state.set_balance('BTC', .5)
        local = {'positions': [{'id': 'l', 'direction': 'long', 'symbol': 'BTCUSDT', 'quantity': 1, 'entry_price': 100}]}
        mismatch = result(fake, local=local, symbol='ETHUSDT', capacity={'current': 1, 'operational_max': 2})
        self.assertEqual(mismatch['status'], 'BLOCKED_POSITION_MISMATCH')
        self.assertIn('BLOCKED_MISSING_PROTECTION', mismatch['blocking_reasons'])
        fake.state.set_balance('BTC', 0)
        self.assertEqual(result(fake, local=local, symbol='ETHUSDT', capacity={'current': 1, 'operational_max': 2})['status'], 'BLOCKED_POSITION_MISMATCH')
        orphan = fake_client(); orphan.create_futures_order({'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET', 'quantity': '.1'})
        self.assertEqual(result(orphan, side='SHORT', symbol='ETHUSDT')['status'], 'BLOCKED_ORPHAN_POSITION')
        timeout = fake_client(); timeout.state.queue_error('futures_account', TimeoutError('timeout'))
        self.assertEqual(result(timeout)['status'], 'BLOCKED_EXCHANGE_STATE_UNKNOWN')

    def test_duplicate_symbol_blocks_only_that_symbol(self):
        fake = fake_client(); local = {'positions': [{'id': 'l', 'direction': 'long', 'symbol': 'BTCUSDT', 'quantity': 1, 'entry_price': 100}]}
        fake.state.set_balance('BTC', 1)
        fake.create_oco({'symbol': 'BTCUSDT', 'side': 'SELL', 'quantity': '1', 'price': 110, 'stopPrice': 90, 'stopLimitPrice': 89})
        same = result(fake, local=local, capacity={'current': 1, 'operational_max': 2})
        other = result(fake, local=local, symbol='ETHUSDT', capacity={'current': 1, 'operational_max': 2})
        self.assertEqual(same['status'], 'BLOCKED_DUPLICATE_SYMBOL'); self.assertTrue(other['safe_to_enter'])

    def test_gate_is_no_network_with_prefetched_context_and_no_mutation(self):
        fake = fake_client(); observation = gate.collect_exchange_observation(fake, now=1700000000)
        before = copy.deepcopy(fake.state.snapshot()); calls = len(fake.calls)
        with patch.object(socket, 'socket', side_effect=AssertionError('network')), patch.object(socket, 'getaddrinfo', side_effect=AssertionError('dns')):
            evaluated = gate.evaluate_pre_entry_safety(local_state={'positions': []}, bot_state={}, side='LONG', symbol='BTCUSDT', now=1700000000,
                context={'exchange_observation': observation, 'capacity': {'current': 0, 'operational_max': 2}})
        self.assertTrue(evaluated['safe_to_enter']); self.assertEqual(calls, len(fake.calls)); self.assertEqual(before, fake.state.snapshot())

    def test_timeline_and_state_summary_are_caller_owned(self):
        fake = fake_client(); local = {'positions': []}; evaluated = result(fake)
        with patch.object(pre_entry_gate_observability.decision_timeline, 'record_event') as record:
            summary = pre_entry_gate_observability.record_result(evaluated, local, cycle_id='c')
        self.assertEqual(local['_last_pre_entry_gate'], summary)
        self.assertEqual(record.call_args.args[0], 'pre_entry_safety_gate')


    def test_bot_state_exposes_compact_last_gate_summary(self):
        summary = {"status": "BLOCKED_CAPACITY", "symbol": "BTCUSDT", "side": "LONG",
                   "observed_at": "2026-07-21T00:00:00Z", "mode": "AUDIT_ONLY",
                   "safe_to_enter": False, "blocking_reasons": ["BLOCKED_CAPACITY"]}
        payload = bot_state.build_bot_state(
            state={"positions": [], "_last_pre_entry_gate": summary},
            btc_ctx={"trend": "neutral", "btc_price": 100, "change_4h": 0},
            spot_real=100, futures_real=100, max_longs=2, max_shorts=2,
        )
        self.assertEqual(payload["last_pre_entry_gate_status"], "BLOCKED_CAPACITY")
        self.assertEqual(payload["pre_entry_safety_summary"], summary)


if __name__ == '__main__': unittest.main()
