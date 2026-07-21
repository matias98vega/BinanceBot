#!/usr/bin/env python3
import os
import socket
import sys
import unittest
from decimal import Decimal
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import pre_entry_safety_gate
from testing.replay_client import ReplayClient
from testing.replay_scenario_runner import ReplayScenarioRunner
from testing.replay_scenarios import build_replay_scenario


class ReplayEndToEndTests(unittest.TestCase):
    def test_spot_order_oco_fill_lifecycle_reuses_fake_engine(self):
        client = ReplayClient(build_replay_scenario('spot-long-tp'))
        with patch.object(socket, 'socket', side_effect=AssertionError('network attempted')), \
             patch.object(socket, 'getaddrinfo', side_effect=AssertionError('DNS attempted')):
            result = client.run_to_end()
        self.assertTrue(result['cursor']['done'])
        self.assertEqual(client.state.order_lists[500]['listOrderStatus'], 'ALL_DONE')
        self.assertGreater(client.state.balance('USDT')['free'], Decimal('100'))
        self.assertEqual(client.operational_events[0]['reason_code'], 'TP_FILLED')

    def test_runner_executes_contract_cycle_per_timestamp_batch(self):
        observations = []
        def cycle(client, events):
            view = {
                'price': client.get_spot_price('BTCUSDT'), 'spot': client.get_spot_account(),
                'futures': client.futures_account(), 'open_orders': client.spot_open_orders({}),
                'events': len(events),
            }
            observations.append(view); return {'price': view['price']}
        result = ReplayScenarioRunner(build_replay_scenario('spot-long-tp'), cycle).run()
        self.assertEqual(len(result['cycles']), 5)
        self.assertEqual(observations[-1]['price'], 110)
        self.assertEqual(result['cycles'][-1]['result'], {'price': 110.0})

    def test_pre_entry_gate_audit_only_uses_same_client_contract(self):
        client = ReplayClient(build_replay_scenario('spot-long-tp'))
        result = pre_entry_safety_gate.evaluate_pre_entry_safety(client=client, local_state={'positions': []}, mode='AUDIT_ONLY')
        self.assertEqual(result['mode'], 'AUDIT_ONLY')
        self.assertTrue(result['entry_allowed'])

    def test_error_pause_and_reconciliation_sequence_is_deterministic(self):
        client = ReplayClient(build_replay_scenario('futures-error-recovery'))
        first = client.step()
        self.assertEqual([event.event_type for event in first], ['ERROR', 'PAUSE'])
        with self.assertRaisesRegex(Exception, 'fixture timeout'):
            client.create_futures_order({'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET', 'quantity': '.1'})
        client.step()
        self.assertEqual(client.reconciliation_events[-1]['status'], 'ALIGNED')
        self.assertEqual(client.operational_events[-1]['state'], 'RUNNING')


if __name__ == '__main__': unittest.main()
