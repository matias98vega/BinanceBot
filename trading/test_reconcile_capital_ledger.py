import json
import os
import tempfile
import unittest

from unittest.mock import patch

import capital_accounting
import capital_ledger
import reconcile_capital_ledger as reconcile


def observation(spot_upnl=0.0, futures_upnl=0.0, positions=None, **changes):
    value = {
        'timestamp': '2026-07-20T12:00:00Z',
        'observation_source': 'test_read_only',
        'observation_age_seconds': 0.0,
        'spot_real': 80.0,
        'futures_real': 20.0,
        'observed_equity': 100.0,
        'baseline_spot_unrealized_pnl': spot_upnl,
        'baseline_futures_unrealized_pnl': futures_upnl,
        'baseline_unrealized_pnl': spot_upnl + futures_upnl,
        'open_positions_at_bootstrap': positions or [],
        'bot_version': 'test',
        'transfer_active': False,
        'errors': [],
        'rebalance': {'persistent_pending': False, 'derived_status': 'PENDING', 'derived_only': True},
    }
    value.update(changes)
    return value


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = os.path.join(self.tmp.name, 'capital_ledger.jsonl')

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_positions_and_spot_positive_negative_futures_combined(self):
        for spot, futures in ((0, 0), (5, 0), (-3, 0), (0, 2), (4, -1)):
            plan = reconcile.build_plan(self.ledger, observation(spot, futures))
            self.assertTrue(plan['valid'])
            self.assertEqual(plan['observation']['baseline_unrealized_pnl'], spot + futures)

    def test_incomplete_stale_transfer_and_equity_mismatch_block(self):
        cases = [
            observation(errors=['missing_entry_price:ADAUSDT'], baseline_unrealized_pnl=None),
            observation(observation_age_seconds=301),
            observation(transfer_active=True, errors=['transfer_in_progress']),
            observation(observed_equity=110),
        ]
        for value in cases:
            self.assertFalse(reconcile.build_plan(self.ledger, value)['valid'])

    def test_changed_plan_blocks_without_write(self):
        first = observation()
        second = observation(spot_real=81, observed_equity=101)
        result = reconcile.apply_plan(self.ledger, first, observer=lambda: second)
        self.assertFalse(result['applied'])
        self.assertFalse(os.path.exists(self.ledger))

    def test_atomic_apply_is_idempotent(self):
        value = observation(5, -1, [{'symbol': 'ADAUSDT', 'wallet': 'SPOT', 'unrealized_pnl': 5}, {'symbol': 'BTCUSDT', 'wallet': 'FUTURES', 'unrealized_pnl': -1}])
        first = reconcile.apply_plan(self.ledger, value, observer=lambda: value)
        second = reconcile.apply_plan(self.ledger, value, observer=lambda: value)
        self.assertTrue(first['applied'])
        self.assertTrue(second['idempotent'])
        self.assertEqual(len(capital_ledger.read_history(self.ledger)), 1)


class BootstrapAccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = os.path.join(self.tmp.name, 'capital_ledger.jsonl')
        plan = reconcile.build_plan(self.ledger, observation(10, 0, [{'symbol': 'ADAUSDT', 'wallet': 'SPOT', 'unrealized_pnl': 10}]))
        reconcile._atomic_write_initial(plan)

    def tearDown(self):
        self.tmp.cleanup()

    def test_close_does_not_duplicate_baseline_and_prebootstrap_is_excluded(self):
        capital_ledger.register_realized_pnl(99, timestamp='2026-07-19T00:00:00Z', ledger_file=self.ledger)
        capital_ledger.register_realized_pnl(12, timestamp='2026-07-21T00:00:00Z', ledger_file=self.ledger)
        capital_ledger.register_commission(2, timestamp='2026-07-21T00:00:00Z', ledger_file=self.ledger)
        capital_ledger.register_funding_fee(-1, timestamp='2026-07-21T00:00:00Z', ledger_file=self.ledger)
        summary = capital_accounting.get_accounting_summary(101, ledger_file=self.ledger, unrealized_pnl=0)
        self.assertEqual(summary['realized_pnl_net_of_fees'], 12)
        self.assertEqual(summary['trading_fees_informational'], 2)
        self.assertEqual(summary['funding_net'], -1)
        self.assertEqual(summary['expected_equity'], 101)
        self.assertEqual(summary['unrealized_change_since_bootstrap'], -10)
        self.assertTrue(summary['pre_bootstrap_activity_excluded'])


if __name__ == '__main__':
    unittest.main()
