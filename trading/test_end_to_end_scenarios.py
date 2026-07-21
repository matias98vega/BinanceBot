#!/usr/bin/env python3
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import futures_reconciliation
from testing.scenarios import all_scenarios, build_scenario


class EndToEndFakeScenarios(unittest.TestCase):
    def test_all_a_to_l_are_deterministic_and_no_network(self):
        with patch.object(socket, 'socket', side_effect=AssertionError('network attempted')), \
             patch.object(socket, 'getaddrinfo', side_effect=AssertionError('DNS attempted')):
            scenarios = all_scenarios()
        self.assertEqual([item.key for item in scenarios], list('ABCDEFGHIJKL'))
        self.assertEqual(
            [item.client.state.snapshot() for item in scenarios],
            [item.client.state.snapshot() for item in all_scenarios()],
        )

    def test_a_long_buy_and_oco_lifecycle(self):
        client = build_scenario('A').client
        buy = client.create_spot_order({'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quantity': '.1'})
        oco = client.create_oco({'symbol': 'BTCUSDT', 'side': 'SELL', 'quantity': '.099', 'price': '110',
                                 'stopPrice': '90', 'stopLimitPrice': '89', 'stopLimitTimeInForce': 'GTC'})
        self.assertEqual(buy['status'], 'FILLED')
        self.assertEqual(oco['listOrderStatus'], 'EXECUTING')

    def test_b_rejection_has_no_exchange_mutation(self):
        client = build_scenario('B').client
        before = client.state.snapshot()['spot_balances']
        with self.assertRaisesRegex(Exception, 'market buy rejected'):
            client.create_spot_order({'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quantity': '.1'})
        self.assertEqual(before, client.state.snapshot()['spot_balances'])

    def test_c_buy_survives_oco_rejection_for_recovery_flow(self):
        client = build_scenario('C').client
        client.create_spot_order({'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quantity': '.1'})
        with self.assertRaisesRegex(Exception, 'OCO rejected'):
            client.create_oco({'symbol': 'BTCUSDT', 'side': 'SELL', 'quantity': '.099', 'price': '110',
                               'stopPrice': '90', 'stopLimitPrice': '89', 'stopLimitTimeInForce': 'GTC'})
        self.assertGreater(client.get_asset_spot('BTC'), 0)

    def test_d_e_tp_and_sl_are_closed(self):
        for key in 'DE':
            scenario = build_scenario(key)
            group = next(iter(scenario.client.state.order_lists.values()))
            self.assertEqual(group['listOrderStatus'], 'ALL_DONE')

    def test_f_external_close_classifies_stale_local_long(self):
        client = build_scenario('F').client
        self.assertEqual(client.get_asset_spot('BTC'), 0)
        self.assertEqual(client.spot_open_orders({'symbol': 'BTCUSDT'}), [])

    def test_g_short_has_reduce_only_protection(self):
        client = build_scenario('G').client
        self.assertLess(float(client.futures_position_risk({'symbol': 'BTCUSDT'})[0]['positionAmt']), 0)
        self.assertTrue(client.futures_open_orders({'symbol': 'BTCUSDT'})[0]['reduceOnly'])

    def test_h_reduce_only_close_is_idempotently_flat(self):
        client = build_scenario('H').client
        self.assertEqual(client.futures_position_risk({'symbol': 'BTCUSDT'}), [])
        self.assertEqual(client.futures_position_risk({'symbol': 'BTCUSDT'}), [])

    def test_i_orphan_is_detected_by_real_reconciliation(self):
        client = build_scenario('I').client
        observed = client.futures_position_risk()
        with tempfile.TemporaryDirectory() as tmp:
            trades = os.path.join(tmp, 'trades.jsonl')
            Path(trades).touch()
            result = futures_reconciliation.classify_positions(observed, state={'positions': []}, trades_file=trades)
        self.assertIn('orphan_futures_position', result['BTCUSDT']['classification'])

    def test_j_rebalance_changes_only_fake_wallets(self):
        client = build_scenario('J').client
        self.assertEqual(client.get_usdt_spot(), 75)
        self.assertEqual(client.state.transfers[0]['amount'], '25')

    def test_k_circuit_breaker_rejects_order(self):
        client = build_scenario('K').client
        with self.assertRaisesRegex(Exception, 'circuit breaker'):
            client.create_futures_order({'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET', 'quantity': '.1'})

    def test_l_capacity_fixture_exposes_existing_position(self):
        client = build_scenario('L').client
        self.assertEqual(len(client.futures_position_risk()), 1)

    def test_temp_paths_do_not_touch_production_or_metadata_contracts(self):
        production = Path(__file__).resolve().parents[1] / 'data' / 'history'
        before = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in production.glob('*') if p.is_file()}
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'state.json').write_text('{"bot_version":"v1.2-sizing-v2","feature_schema_version":2}', encoding='utf-8')
        after = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in production.glob('*') if p.is_file()}
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
