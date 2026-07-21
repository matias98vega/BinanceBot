#!/usr/bin/env python3
import json
import os
import socket
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

from testing import ReplayClient
from testing.build_replay_fixture import main as fixture_main
from testing.replay_fixture_library import (
    CONFIDENCE_LEVELS,
    FIDELITY_LEVELS,
    REQUIRED_MARKERS,
    SanitizedReplayFixture,
    get_fixture,
    load_library,
    validate_fixture,
)
from testing.run_replay_scenario import main as replay_main


def fixture_checkpoint(scenario_id, checkpoint_id):
    checkpoints = get_fixture(scenario_id).as_dict()['checkpoints']
    return next(row['expected'] for row in checkpoints if row['id'] == checkpoint_id)


EXPECTED_INCIDENTS = {
    'incident-ada-stale-spot',
    'incident-sol-orphan-futures',
    'incident-ondo-xrp-partial-evidence',
    'incident-oco-failure-after-fill',
    'incident-external-spot-close-with-dust',
    'incident-order-timeout-unknown-result',
}


class ReplayIncidentFixtureLibraryTests(unittest.TestCase):
    def test_library_contains_six_valid_sanitized_offline_incidents(self):
        library = load_library()
        self.assertEqual(set(library), EXPECTED_INCIDENTS)
        for fixture in library.values():
            payload = fixture.as_dict()
            self.assertFalse(validate_fixture(payload))
            self.assertIn(payload['fidelity'], FIDELITY_LEVELS)
            self.assertIn(payload['confidence'], CONFIDENCE_LEVELS)
            self.assertTrue(REQUIRED_MARKERS.issubset(payload['data_classification']))
            self.assertNotEqual(payload['fidelity'], 'FULL_FIDELITY')
            self.assertTrue(payload['source_files'])
            self.assertTrue(payload['synthetic_fields'] or payload['known_missing_fields'])

    def test_fixture_and_tape_are_immutable_and_deterministic(self):
        first = get_fixture('incident-ada-stale-spot')
        second = get_fixture('incident-ada-stale-spot')
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.tape.fingerprint, second.tape.fingerprint)
        with self.assertRaises(TypeError):
            first.payload['initial_state']['prices']['ADAUSDT'] = '999'

    def test_validation_rejects_false_fidelity_and_missing_markers(self):
        payload = get_fixture('incident-ada-stale-spot').as_dict()
        payload['fidelity'] = 'FULL_FIDELITY'
        self.assertIn('FULL_FIDELITY cannot declare known_missing_fields', validate_fixture(payload))
        payload['fidelity'] = 'CONTROL_FLOW_FIDELITY'
        payload['data_classification'].remove('NO_NETWORK')
        self.assertTrue(any('NO_NETWORK' in error for error in validate_fixture(payload)))

    def test_builder_validates_without_writing_and_exports_only_explicitly(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(StringIO()) as output:
            self.assertEqual(fixture_main(['--validate-all', '--json']), 0)
            self.assertEqual(os.listdir(tmp), [])
            result = json.loads(output.getvalue())
            self.assertEqual((result['valid'], result['count']), (True, 6))
            destination = os.path.join(tmp, 'export')
            self.assertEqual(fixture_main(['--fixture', 'incident-ada-stale-spot', '--output', destination]), 0)
            self.assertEqual(os.listdir(destination), ['incident-ada-stale-spot.tape.json'])

    def test_all_incidents_run_without_network_and_preserve_partial_fidelity(self):
        with patch.object(socket, 'socket', side_effect=AssertionError('network attempted')), \
             patch.object(socket, 'getaddrinfo', side_effect=AssertionError('DNS attempted')):
            for scenario_id, fixture in load_library().items():
                with self.subTest(scenario_id=scenario_id):
                    client = ReplayClient(fixture.tape)
                    result = client.run_to_end()
                    self.assertTrue(result['cursor']['done'])
                    self.assertFalse(client.network_allowed)
                    self.assertFalse(client.fidelity['complete'])

    def test_ada_stale_and_external_close_preserve_dust_without_writes(self):
        for scenario_id, asset in (
            ('incident-ada-stale-spot', 'ADA'),
            ('incident-external-spot-close-with-dust', 'LINK'),
        ):
            client = ReplayClient(get_fixture(scenario_id).tape)
            client.run_to_end()
            self.assertGreater(client.state.balance(asset)['free'], 0)
            self.assertEqual([call for call in client.calls if ':POST:' in call['operation']], [])
            self.assertEqual(fixture_checkpoint(scenario_id, 'classification'), 'CLOSED_ON_EXCHANGE_OPEN_IN_STATE')

    def test_sol_orphan_remains_observation_only_and_unprotected(self):
        client = ReplayClient(get_fixture('incident-sol-orphan-futures').tape)
        client.run_to_end()
        self.assertEqual(client.futures_position_risk()[0]['symbol'], 'SOLUSDT')
        self.assertEqual(client.futures_open_orders({'symbol': 'SOLUSDT'}), [])
        self.assertIn('orphan_futures_position', client.reconciliation_events[0]['classification'])
        self.assertIn('unprotected_futures_position', client.reconciliation_events[0]['classification'])
        self.assertEqual([call for call in client.calls if ':POST:' in call['operation']], [])

    def test_ondo_xrp_is_explicit_analytics_only_partial_evidence(self):
        fixture = get_fixture('incident-ondo-xrp-partial-evidence')
        client = ReplayClient(fixture.tape)
        client.run_to_end()
        self.assertEqual(fixture.as_dict()['fidelity'], 'ANALYTICS_ONLY')
        self.assertEqual(len(client.operational_events), 2)
        self.assertEqual(fixture_checkpoint('incident-ondo-xrp-partial-evidence', 'classification'), 'LEGACY_NO_EVIDENCE')
        self.assertTrue(fixture.as_dict()['known_missing_fields'])

    def test_oco_failure_happens_after_fill_without_rolling_back_asset(self):
        client = ReplayClient(get_fixture('incident-oco-failure-after-fill').tape)
        client.step()
        asset_after_fill = client.state.balance('BTC')['free']
        self.assertGreater(asset_after_fill, 0)
        client.step()
        with self.assertRaisesRegex(Exception, 'OCO'):
            client.create_oco({'symbol': 'BTCUSDT', 'quantity': '.1', 'price': '110', 'stopPrice': '90'})
        self.assertEqual(client.state.balance('BTC')['free'], asset_after_fill)
        self.assertEqual(client.state.order_lists, {})

    def test_timeout_is_unknown_not_a_failed_order_or_retry(self):
        client = ReplayClient(get_fixture('incident-order-timeout-unknown-result').tape)
        client.step()
        with self.assertRaises(TimeoutError):
            client.create_spot_order({'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quantity': '.1'})
        self.assertEqual(client.state.orders, {})
        client.step()
        self.assertIn('UNKNOWN', json.dumps(client.operational_events))
        self.assertEqual(len([call for call in client.calls if ':POST:' in call['operation']]), 1)

    def test_incident_cli_is_compact_and_strict_rejects_partial(self):
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(replay_main(['--incident', 'incident-ada-stale-spot', '--json']), 0)
        summary = json.loads(output.getvalue())
        self.assertEqual(summary['scenario_id'], 'incident-ada-stale-spot')
        self.assertFalse(summary['complete'])
        self.assertFalse(summary['network_fallback'])
        with redirect_stdout(StringIO()):
            self.assertEqual(replay_main(['--incident', 'incident-ada-stale-spot', '--strict']), 2)


if __name__ == '__main__':
    unittest.main()
