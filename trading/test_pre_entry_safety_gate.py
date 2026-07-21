#!/usr/bin/env python3
import copy
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))

import pre_entry_safety_gate as gate
from testing import FakeBinanceClient, FakeExchangeState


def client():
    state = FakeExchangeState()
    state.set_balance('USDT', 100)
    state.set_price('BTCUSDT', 100)
    state.set_price('ETHUSDT', 20)
    return FakeBinanceClient(state)


def evaluate(fake=None, state=None, side='LONG', symbol='BTCUSDT', now=1700000000, **changes):
    fake = fake or client()
    context = {'capacity': {'current': 0, 'operational_max': 2, 'target_max': 2, 'new_entries_allowed': True}}
    context.update(changes.pop('context', {}))
    return gate.evaluate_pre_entry_safety(client=fake, local_state=state or {'positions': []}, bot_state={}, side=side,
                                          symbol=symbol, context=context, now=now, **changes)


class PreEntrySafetyGateTests(unittest.TestCase):
    def test_safe_result_and_all_canonical_checks(self):
        result = evaluate()
        self.assertTrue(result['safe_to_enter'])
        self.assertEqual(result['status'], gate.SAFE)
        self.assertEqual(12, len(result['checks']))

    def test_local_state_invalid(self):
        result = evaluate(state={'positions': 'bad'})
        self.assertEqual(result['status'], 'BLOCKED_LOCAL_STATE_INVALID')

    def test_exchange_error_and_unknown_order_state_fail_closed(self):
        fake = client(); fake.state.queue_error('get_spot_account', TimeoutError('timeout'))
        result = evaluate(fake=fake)
        self.assertEqual(result['status'], 'BLOCKED_EXCHANGE_STATE_UNKNOWN')
        self.assertIn('BLOCKED_BALANCE_UNRELIABLE', result['blocking_reasons'])

    def test_stale_context_blocks_without_client_reads(self):
        fake = client()
        observation = gate.collect_exchange_observation(fake, now=100)
        calls = len(fake.calls)
        result = evaluate(fake=None, context={'exchange_observation': observation}, now=1000, max_age_seconds=10)
        self.assertEqual(result['freshness']['exchange'], 'STALE')
        self.assertEqual(result['status'], 'BLOCKED_EXCHANGE_STATE_UNKNOWN')
        self.assertEqual(calls, len(fake.calls))

    def test_spot_quantity_mismatch_and_missing_protection(self):
        fake = client(); fake.state.set_balance('BTC', .5)
        state = {'positions': [{'id': 'x', 'direction': 'long', 'symbol': 'BTCUSDT', 'quantity': 1, 'entry_price': 100}]}
        result = evaluate(fake=fake, state=state, context={'capacity': {'current': 1, 'operational_max': 2}})
        self.assertEqual(result['status'], 'BLOCKED_POSITION_MISMATCH')
        self.assertIn('BLOCKED_MISSING_PROTECTION', result['blocking_reasons'])

    def test_spot_complete_oco_is_protected(self):
        fake = client(); fake.state.set_balance('BTC', 1)
        fake.create_oco({'symbol': 'BTCUSDT', 'side': 'SELL', 'quantity': '1', 'price': '110', 'stopPrice': '90', 'stopLimitPrice': '89', 'stopLimitTimeInForce': 'GTC'})
        state = {'positions': [{'id': 'x', 'direction': 'long', 'symbol': 'BTCUSDT', 'quantity': 1, 'entry_price': 100}]}
        result = evaluate(fake=fake, state=state, symbol='ETHUSDT', context={'capacity': {'current': 1, 'operational_max': 2}})
        self.assertTrue(result['safe_to_enter'])

    def test_spot_dust_is_not_orphan_or_duplicate_for_other_symbol(self):
        fake = client(); fake.state.set_balance('BTC', .00001)
        result = evaluate(fake=fake, symbol='ETHUSDT')
        self.assertTrue(result['safe_to_enter'])

    def test_futures_orphan_has_priority(self):
        fake = client()
        fake.create_futures_order({'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET', 'quantity': '.1'})
        result = evaluate(fake=fake, side='SHORT', symbol='ETHUSDT')
        self.assertEqual(result['status'], 'BLOCKED_ORPHAN_POSITION')

    def test_futures_side_inversion_is_mismatch(self):
        fake = client()
        fake.create_futures_order({'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quantity': '.1'})
        state = {'positions': [{'id': 's', 'direction': 'short', 'symbol': 'BTCUSDT', 'quantity': .1, 'entry_price': 100}]}
        result = evaluate(fake=fake, state=state, side='SHORT', symbol='ETHUSDT', context={'capacity': {'current': 1, 'operational_max': 2}})
        self.assertEqual(result['status'], 'BLOCKED_POSITION_MISMATCH')

    def test_futures_reduce_only_protection(self):
        fake = client()
        fake.create_futures_order({'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET', 'quantity': '.1'})
        fake.create_futures_order({'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'LIMIT', 'quantity': '.1', 'price': 90, 'reduceOnly': 'true'})
        state = {'positions': [{'id': 's', 'direction': 'short', 'symbol': 'BTCUSDT', 'quantity': .1, 'entry_price': 100}]}
        result = evaluate(fake=fake, state=state, side='SHORT', symbol='ETHUSDT', context={'capacity': {'current': 1, 'operational_max': 2}})
        self.assertTrue(result['safe_to_enter'])

    def test_pending_position_reconciliation_blocks_but_benign_rebalance_does_not(self):
        blocked = evaluate(reconciliation_status={'position_pending': True})
        self.assertEqual(blocked['status'], 'BLOCKED_RECONCILIATION_PENDING')
        safe = evaluate(reconciliation_status={'capital_rebalance_pending': True, 'aligned': True})
        self.assertTrue(safe['safe_to_enter'])

    def test_capacity_full(self):
        result = evaluate(context={'capacity': {'current': 2, 'operational_max': 2, 'new_entries_allowed': False}})
        self.assertEqual(result['status'], 'BLOCKED_CAPACITY')

    def test_duplicate_symbol_and_cooldown(self):
        state = {'positions': [], 'cooldown_symbols': {'BTCUSDT': 1700001000}}
        result = evaluate(state=state)
        self.assertEqual(result['status'], 'BLOCKED_DUPLICATE_SYMBOL')

    def test_active_risk_has_highest_priority(self):
        state = {'positions': [], 'status': 'paused'}
        result = evaluate(state=state, context={'capacity': {'current': 2, 'operational_max': 2}})
        self.assertEqual(result['status'], 'BLOCKED_ACTIVE_RISK_STATE')
        self.assertIn('BLOCKED_CAPACITY', result['blocking_reasons'])

    def test_balance_unreliable(self):
        observation = gate.collect_exchange_observation(client(), now=1700000000)
        observation['spot_account']['balances'][0]['free'] = '-1'
        result = evaluate(context={'exchange_observation': observation})
        self.assertEqual(result['status'], 'BLOCKED_BALANCE_UNRELIABLE')

    def test_audit_only_allows_but_enforce_rejects(self):
        audit = evaluate(state={'positions': 'bad'}, mode=gate.AUDIT_ONLY)
        enforce = evaluate(state={'positions': 'bad'}, mode=gate.ENFORCE)
        self.assertTrue(audit['entry_allowed']); self.assertFalse(enforce['entry_allowed'])

    def test_idempotent_no_mutation_and_fast(self):
        fake, state = client(), {'positions': []}
        before_fake, before_state = fake.state.snapshot(), copy.deepcopy(state)
        one = evaluate(fake=fake, state=state); calls_after_one = len(fake.calls)
        observation = gate.collect_exchange_observation(client(), now=1700000000)
        two = evaluate(state=state, context={'exchange_observation': observation})
        three = evaluate(state=state, context={'exchange_observation': observation})
        self.assertEqual(two['status'], three['status'])
        self.assertEqual(before_state, state)
        self.assertEqual(before_fake['orders'], fake.state.snapshot()['orders'])
        self.assertLess(two['duration_ms'], 10)
        self.assertGreater(calls_after_one, 0)

    def test_rejection_reason_is_canonical(self):
        self.assertEqual(gate.rejection_reason({'status': 'BLOCKED_CAPACITY'}), 'PRE_ENTRY_GATE_CAPACITY')


if __name__ == '__main__': unittest.main()
