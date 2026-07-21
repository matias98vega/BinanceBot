#!/usr/bin/env python3
import json
import os
import socket
import sys
import tempfile
import unittest
from decimal import Decimal
from io import StringIO
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

from testing import FakeBinanceError, ReplayClient, ReplayTape
from testing.replay_scenarios import START_MS, STARTED_AT, historical_event_tape, recorded_observation_tape
from testing.run_replay_scenario import main as replay_main


def tape(events=(), initial=None, mode='FIXTURE_REPLAY', missing=()):
    return ReplayTape.from_dict({
        'replay_schema_version': 1, 'scenario_id': 'unit', 'mode': mode, 'timezone': 'UTC',
        'started_at': STARTED_AT, 'initial_state': initial or {'prices': {'BTCUSDT': '100'}},
        'events': list(events), 'missing_fields': list(missing),
    })


class ReplayClientTests(unittest.TestCase):
    def test_tape_is_versioned_ordered_and_fingerprinted(self):
        source = {'symbol': 'BTCUSDT', 'price': '101'}
        one = tape([{'at_ms': START_MS + 1, 'event_type': 'PRICE', 'payload': source}])
        source['price'] = '999'
        two = ReplayTape.from_dict(one.as_dict())
        self.assertEqual(one.fingerprint, two.fingerprint)
        self.assertEqual(one.events[0].payload['price'], '101')
        nested = tape([{'at_ms': START_MS + 1, 'event_type': 'SPOT_ORDER', 'payload': {'params': {'quantity': '.1'}}}])
        with self.assertRaises(TypeError): nested.events[0].payload['params']['quantity'] = '2'
        with self.assertRaisesRegex(ValueError, 'ordered'):
            tape([{'at_ms': START_MS + 2, 'event_type': 'PRICE', 'payload': source},
                  {'at_ms': START_MS + 1, 'event_type': 'PRICE', 'payload': source}])
        with self.assertRaisesRegex(ValueError, 'schema'):
            ReplayTape.from_dict({'replay_schema_version': 2, 'scenario_id': 'x', 'started_at': STARTED_AT})

    def test_fixture_cannot_claim_missing_fields(self):
        with self.assertRaisesRegex(ValueError, 'missing_fields'):
            tape(mode='FIXTURE_REPLAY', missing=['balances'])

    def test_initial_snapshots_advance_deterministic_ids(self):
        source = tape(initial={
            'prices': {'BTCUSDT': '100'}, 'balances': {'USDT': {'free': '100'}},
            'open_orders': [{'symbol': 'BTCUSDT', 'orderId': 1500, 'status': 'FILLED'}],
            'order_lists': [{'orderListId': 900, 'orders': []}], 'trades': [{'id': 40, 'symbol': 'BTCUSDT'}],
        })
        client = ReplayClient(source)
        self.assertEqual((client.state.next_order_id, client.state.next_list_id, client.state.next_trade_id), (1501, 901, 41))

    def test_cursor_clock_steps_and_cannot_move_backwards(self):
        client = ReplayClient(tape([
            {'at_ms': START_MS + 1_000, 'event_type': 'PRICE', 'payload': {'symbol': 'BTCUSDT', 'price': '101'}},
            {'at_ms': START_MS + 2_000, 'event_type': 'PRICE', 'payload': {'symbol': 'BTCUSDT', 'price': '102'}},
        ]))
        self.assertEqual(client.get_price('BTCUSDT'), 100)
        client.step(); self.assertEqual(client.get_price('BTCUSDT'), 101)
        client.advance(1); self.assertEqual(client.get_price('BTCUSDT'), 102)
        with self.assertRaisesRegex(ValueError, 'backwards'): client.advance_to(START_MS)

    def test_state_observations_cover_klines_balances_positions_orders_and_fills(self):
        events = [
            {'at_ms': START_MS + 1, 'event_type': 'KLINES', 'payload': {'symbol': 'BTCUSDT', 'rows': [[1, '1', '2', '1', '2']]}},
            {'at_ms': START_MS + 2, 'event_type': 'BALANCE', 'payload': {'asset': 'USDT', 'free': '42'}},
            {'at_ms': START_MS + 3, 'event_type': 'FUTURES_WALLET', 'payload': {'balance': '55'}},
            {'at_ms': START_MS + 4, 'event_type': 'FUTURES_POSITION', 'payload': {'symbol': 'BTCUSDT', 'positionAmt': '-.1', 'entryPrice': '100', 'leverage': 2}},
            {'at_ms': START_MS + 5, 'event_type': 'ORDER_SNAPSHOT', 'payload': {'symbol': 'BTCUSDT', 'orderId': 7, 'status': 'NEW'}},
            {'at_ms': START_MS + 6, 'event_type': 'FILL_SNAPSHOT', 'payload': {'order': {'symbol': 'BTCUSDT', 'orderId': 7, 'status': 'FILLED'}, 'trade': {'id': 9, 'symbol': 'BTCUSDT'}, 'balances': {'USDT': {'free': '44'}}}},
        ]
        client = ReplayClient(tape(events)); client.run_to_end()
        self.assertEqual(client.get_klines('BTCUSDT', limit=1)[0][4], '2')
        self.assertEqual(client.get_usdt_spot(), 44)
        self.assertEqual(client.state.futures_wallet_balance, Decimal('55'))
        self.assertEqual(client.futures_position_risk()[0]['positionAmt'], '-0.1')
        self.assertEqual(client.get_spot_order({'orderId': 7})['status'], 'FILLED')
        self.assertEqual(client.state.trades[0]['id'], 9)

    def test_errors_use_fake_queue_and_preserve_error_contract(self):
        client = ReplayClient(tape([{'at_ms': START_MS + 1, 'event_type': 'ERROR', 'payload': {
            'operation': 'get_spot_price', 'message': 'offline timeout', 'code': -1007, 'status': 504,
        }}]))
        client.step()
        with self.assertRaises(FakeBinanceError) as raised: client.get_spot_price('BTCUSDT')
        self.assertEqual((raised.exception.code, raised.exception.status), (-1007, 504))
        self.assertEqual(client.get_spot_price('BTCUSDT'), 100)

    def test_recorded_and_historical_modes_are_explicitly_partial(self):
        recorded = recorded_observation_tape('r', STARTED_AT, {'prices': {'BTCUSDT': 100}}, [], ['fills'])
        self.assertFalse(ReplayClient(recorded).fidelity['complete'])
        historical = historical_event_tape('h', [{'timestamp': STARTED_AT, 'event_type': 'DECISION', 'symbol': 'BTCUSDT'}])
        client = ReplayClient(historical); client.run_to_end()
        self.assertEqual(client.operational_events[0]['event_type'], 'DECISION')
        self.assertIn('exchange_responses', client.fidelity['missing_fields'])
        self.assertEqual(client.get_usdt_spot(), 0)
        self.assertEqual(client.state.futures_wallet_balance, 0)

    def test_futures_order_event_reuses_fake_reduce_only_semantics(self):
        source = tape([
            {'at_ms': START_MS + 1, 'event_type': 'FUTURES_ORDER', 'payload': {'params': {'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET', 'quantity': '.1'}}},
            {'at_ms': START_MS + 2, 'event_type': 'FUTURES_ORDER', 'payload': {'params': {'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quantity': '.1', 'reduceOnly': 'true'}}},
        ], initial={'prices': {'BTCUSDT': '100'}, 'futures_wallet_balance': '100'})
        client = ReplayClient(source); client.run_to_end()
        self.assertEqual(client.futures_position_risk(), [])
        self.assertEqual([row['result']['status'] for row in client.action_results], ['FILLED', 'FILLED'])

    def test_unknown_endpoint_and_network_fail_closed(self):
        client = ReplayClient(tape())
        with patch.object(socket, 'socket', side_effect=AssertionError('network attempted')), \
             patch.object(socket, 'getaddrinfo', side_effect=AssertionError('DNS attempted')):
            self.assertEqual(client.get_price('BTCUSDT'), 100)
            with self.assertRaises(NotImplementedError): client.spot_signed('POST', '/real', {})
        self.assertFalse(client.network_allowed)

    def test_repeated_runs_are_byte_equivalent_after_decimal_normalisation(self):
        source = tape([{'at_ms': START_MS + 1, 'event_type': 'BALANCE', 'payload': {'asset': 'USDT', 'free': '10'}}])
        def normalized(): return json.dumps(ReplayClient(source).run_to_end(), default=str, sort_keys=True)
        self.assertEqual(normalized(), normalized())

    def test_cli_strict_rejects_partial_and_only_writes_explicit_output(self):
        partial = recorded_observation_tape('partial', STARTED_AT, {}, [], ['balances'])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'tape.json')
            with open(path, 'w', encoding='utf-8') as handle: json.dump(partial.as_dict(), handle)
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(replay_main(['--tape', path, '--json', '--strict']), 2)
            self.assertIn('"complete": false', output.getvalue())
            self.assertEqual(os.listdir(tmp), ['tape.json'])


if __name__ == '__main__': unittest.main()
