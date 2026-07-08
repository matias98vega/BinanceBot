#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import residuals


OLD_TS = '2026-07-06T00:36:05Z'
NOW_TS = '2026-07-08T22:25:00Z'


def _status(*entries):
    return {
        'residuals': {
            entry['symbol']: entry
            for entry in entries
        },
        'updated_at': OLD_TS,
    }


def _entry(symbol='NEARUSDT', asset='NEAR', quantity=2.595):
    return {
        'symbol': symbol,
        'asset': asset,
        'quantity': quantity,
        'balance_quantity': quantity,
        'estimated_value': 5.18,
        'min_notional': 5.0,
        'status': 'unprotectable_residual',
        'first_seen': OLD_TS,
        'last_seen': OLD_TS,
    }


class SpotResidualReconciliationTests(unittest.TestCase):
    def test_stale_residual_is_removed_when_balance_is_much_lower(self):
        updated, removed = residuals.reconcile_spot_residual_status_with_balances(
            _status(_entry()),
            [{'asset': 'NEAR', 'free': '0.08810000', 'locked': '0.00000000'}],
            state={'positions': []},
            now=NOW_TS,
            qty_ratio=0.5,
            min_age_seconds=3600,
        )

        self.assertEqual(updated['residuals'], {})
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0]['symbol'], 'NEARUSDT')
        self.assertEqual(removed[0]['reason'], 'actual_balance_below_residual_quantity')

    def test_matching_balance_preserves_residual(self):
        updated, removed = residuals.reconcile_spot_residual_status_with_balances(
            _status(_entry()),
            [{'asset': 'NEAR', 'free': '2.595', 'locked': '0'}],
            state={'positions': []},
            now=NOW_TS,
            qty_ratio=0.5,
            min_age_seconds=3600,
        )

        self.assertIn('NEARUSDT', updated['residuals'])
        self.assertEqual(removed, [])

    def test_locked_balance_preserves_residual(self):
        updated, removed = residuals.reconcile_spot_residual_status_with_balances(
            _status(_entry()),
            [{'asset': 'NEAR', 'free': '0.01', 'locked': '2.0'}],
            state={'positions': []},
            now=NOW_TS,
            qty_ratio=0.5,
            min_age_seconds=3600,
        )

        self.assertIn('NEARUSDT', updated['residuals'])
        self.assertEqual(removed, [])

    def test_other_symbol_is_preserved_when_one_residual_is_cleared(self):
        updated, removed = residuals.reconcile_spot_residual_status_with_balances(
            _status(_entry(), _entry('SOLUSDT', 'SOL', 0.061864)),
            [
                {'asset': 'NEAR', 'free': '0.0881', 'locked': '0'},
                {'asset': 'SOL', 'free': '0.061864', 'locked': '0'},
            ],
            state={'positions': []},
            now=NOW_TS,
            qty_ratio=0.5,
            min_age_seconds=3600,
        )

        self.assertNotIn('NEARUSDT', updated['residuals'])
        self.assertIn('SOLUSDT', updated['residuals'])
        self.assertEqual([item['symbol'] for item in removed], ['NEARUSDT'])

    def test_missing_file_does_not_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'residuals_status.json')

            result = residuals.reconcile_status_file_with_spot_balances(
                path=path,
                balances=[],
                state={'positions': []},
                now=NOW_TS,
            )

        self.assertTrue(result['ok'])
        self.assertEqual(result['removed'], [])

    def test_corrupt_json_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'residuals_status.json')
            with open(path, 'w', encoding='utf-8') as f:
                f.write('{bad json')

            with self.assertLogs(level='WARNING') as logs:
                result = residuals.reconcile_status_file_with_spot_balances(
                    path=path,
                    balances=[{'asset': 'NEAR', 'free': '0', 'locked': '0'}],
                    state={'positions': []},
                    now=NOW_TS,
                )
            with open(path, encoding='utf-8') as f:
                content = f.read()

        self.assertFalse(result['ok'])
        self.assertEqual(content, '{bad json')
        self.assertIn('residual status read failed', '\n'.join(logs.output))

    def test_active_state_position_preserves_nonzero_balance(self):
        updated, removed = residuals.reconcile_spot_residual_status_with_balances(
            _status(_entry()),
            [{'asset': 'NEAR', 'free': '0.08810000', 'locked': '0'}],
            state={'positions': [{'symbol': 'NEARUSDT', 'direction': 'long'}]},
            now=NOW_TS,
            qty_ratio=0.5,
            min_age_seconds=3600,
        )

        self.assertIn('NEARUSDT', updated['residuals'])
        self.assertEqual(removed, [])

    def test_active_state_position_clears_when_balance_is_zero(self):
        updated, removed = residuals.reconcile_spot_residual_status_with_balances(
            _status(_entry()),
            [{'asset': 'NEAR', 'free': '0', 'locked': '0'}],
            state={'positions': [{'symbol': 'NEARUSDT', 'direction': 'long'}]},
            now=NOW_TS,
            qty_ratio=0.5,
            min_age_seconds=3600,
        )

        self.assertEqual(updated['residuals'], {})
        self.assertEqual(removed[0]['reason'], 'actual_balance_below_residual_quantity')

    def test_recent_residual_is_not_removed_in_same_cycle(self):
        recent = _entry()
        recent['last_seen'] = NOW_TS

        updated, removed = residuals.reconcile_spot_residual_status_with_balances(
            _status(recent),
            [{'asset': 'NEAR', 'free': '0.08810000', 'locked': '0'}],
            state={'positions': []},
            now=NOW_TS,
            qty_ratio=0.5,
            min_age_seconds=3600,
        )

        self.assertIn('NEARUSDT', updated['residuals'])
        self.assertEqual(removed, [])

    def test_status_file_is_updated_and_timeline_event_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'residuals_status.json')
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(_status(_entry()), f)

            with patch('decision_timeline.record_event') as record_event:
                result = residuals.reconcile_status_file_with_spot_balances(
                    path=path,
                    balances=[{'asset': 'NEAR', 'free': '0.08810000', 'locked': '0'}],
                    state={'positions': []},
                    now=NOW_TS,
                    qty_ratio=0.5,
                    min_age_seconds=3600,
                )
            saved = residuals.load_status(path)

        self.assertTrue(result['ok'])
        self.assertEqual(saved['residuals'], {})
        record_event.assert_called_once()
        self.assertEqual(record_event.call_args.kwargs['event'], 'spot_residual_stale_cleared')


if __name__ == '__main__':
    unittest.main()
