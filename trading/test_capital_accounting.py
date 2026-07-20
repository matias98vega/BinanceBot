#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import analytics_engine
import capital_accounting
import capital_ledger


class CapitalAccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger_file = os.path.join(self.tmp.name, 'capital_ledger.jsonl')

    def tearDown(self):
        self.tmp.cleanup()

    def seed_movements(self):
        capital_ledger.register_external_deposit(100, ledger_file=self.ledger_file)
        capital_ledger.register_external_deposit(50, ledger_file=self.ledger_file)
        capital_ledger.register_external_withdrawal(20, ledger_file=self.ledger_file)
        capital_ledger.register_commission(0.4, ledger_file=self.ledger_file)
        capital_ledger.register_funding_fee(-0.15, ledger_file=self.ledger_file)
        capital_ledger.register_realized_pnl(12.5, ledger_file=self.ledger_file)
        capital_ledger.register_rebalance(30, ledger_file=self.ledger_file)

    def test_external_deposits_withdrawals_and_net_flows(self):
        self.seed_movements()

        self.assertEqual(capital_accounting.get_external_deposits(self.ledger_file), 150.0)
        self.assertEqual(capital_accounting.get_external_withdrawals(self.ledger_file), 20.0)
        self.assertEqual(capital_accounting.get_net_external_flows(self.ledger_file), 130.0)

    def test_commissions_funding_and_realized_pnl(self):
        self.seed_movements()

        self.assertEqual(capital_accounting.get_total_commissions(self.ledger_file), 0.4)
        self.assertEqual(capital_accounting.get_total_funding(self.ledger_file), -0.15)
        self.assertEqual(capital_accounting.get_realized_trading_pnl(self.ledger_file), 12.5)

    def test_rebalance_does_not_affect_external_flows(self):
        capital_ledger.register_rebalance(99, ledger_file=self.ledger_file)

        self.assertEqual(capital_accounting.get_external_deposits(self.ledger_file), 0.0)
        self.assertEqual(capital_accounting.get_external_withdrawals(self.ledger_file), 0.0)
        self.assertEqual(capital_accounting.get_net_external_flows(self.ledger_file), 0.0)

    def test_adjusted_equity_pnl_and_roi(self):
        self.seed_movements()

        self.assertEqual(capital_accounting.get_adjusted_equity(300, self.ledger_file), 170.0)
        self.assertEqual(capital_accounting.get_adjusted_pnl(300, 100, self.ledger_file), 70.0)
        self.assertEqual(capital_accounting.get_adjusted_roi(300, 100, self.ledger_file), 70.0)

    def test_adjusted_helpers_return_none_for_missing_inputs(self):
        self.assertIsNone(capital_accounting.get_adjusted_equity(None, self.ledger_file))
        self.assertIsNone(capital_accounting.get_adjusted_pnl(None, 100, self.ledger_file))
        self.assertIsNone(capital_accounting.get_adjusted_roi(100, 0, self.ledger_file))

    def test_accounting_summary(self):
        self.seed_movements()

        summary = capital_accounting.get_accounting_summary(300, 100, self.ledger_file)

        self.assertEqual(summary['external_deposits'], 150.0)
        self.assertEqual(summary['external_withdrawals'], 20.0)
        self.assertEqual(summary['net_external_flows'], 130.0)
        self.assertEqual(summary['commissions'], 0.4)
        self.assertEqual(summary['funding'], -0.15)
        self.assertEqual(summary['realized_trading_pnl'], 12.5)
        self.assertEqual(summary['adjusted_equity'], 170.0)
        self.assertEqual(summary['adjusted_pnl'], 70.0)
        self.assertEqual(summary['adjusted_roi'], 70.0)

    def test_asset_filter(self):
        capital_ledger.register_external_deposit(100, asset='USDT', ledger_file=self.ledger_file)
        capital_ledger.register_external_deposit(2, asset='BTC', ledger_file=self.ledger_file)
        capital_ledger.register_external_withdrawal(25, asset='USDT', ledger_file=self.ledger_file)

        self.assertEqual(capital_accounting.get_net_external_flows(self.ledger_file, asset='USDT'), 75.0)
        self.assertEqual(capital_accounting.get_net_external_flows(self.ledger_file, asset='BTC'), 2.0)

    def test_reads_through_capital_ledger_api(self):
        with patch.object(capital_accounting.capital_ledger, 'get_totals_by_type', return_value={
            'external_deposit': 10,
            'external_withdrawal': 3,
        }) as totals:
            net = capital_accounting.get_net_external_flows(self.ledger_file)

        self.assertEqual(net, 7.0)
        totals.assert_called()

    def test_analytics_behavior_is_unchanged(self):
        stats_file = os.path.join(self.tmp.name, 'stats.json')
        trades_file = os.path.join(self.tmp.name, 'trades.jsonl')
        decisions_file = os.path.join(self.tmp.name, 'decisions.jsonl')
        snapshots_file = os.path.join(self.tmp.name, 'snapshots.jsonl')
        features_file = os.path.join(self.tmp.name, 'features.jsonl')
        trade = {
            'event_type': 'TRADE_CLOSE',
            'trade_id': 't1',
            'symbol': 'ETHUSDT',
            'side': 'LONG',
            'status': 'CLOSED',
            'result': 'WIN',
            'pnl_usdt': 10,
            'pnl_pct': 5,
            'exit_reason': 'TP',
            'closed_at': '2026-07-02T10:00:00Z',
        }
        with open(trades_file, 'w', encoding='utf-8') as f:
            f.write(json.dumps(trade) + '\n')

        baseline = analytics_engine.rebuild_statistics(
            trades_file=trades_file,
            decisions_file=decisions_file,
            snapshots_file=snapshots_file,
            stats_file=stats_file,
            features_file=features_file,
        )
        self.seed_movements()
        after = analytics_engine.rebuild_statistics(
            trades_file=trades_file,
            decisions_file=decisions_file,
            snapshots_file=snapshots_file,
            stats_file=stats_file,
            features_file=features_file,
        )

        self.assertEqual(after['general']['pnl_total'], baseline['general']['pnl_total'])
        self.assertEqual(after['general']['win_rate'], baseline['general']['win_rate'])
        self.assertEqual(after['general']['profit_factor'], baseline['general']['profit_factor'])


    def test_net_pnl_convention_does_not_double_subtract_fees(self):
        capital_ledger.record_movement(capital_ledger.TYPE_INITIAL_CAPITAL, 100, reference_id="initial", ledger_file=self.ledger_file)
        capital_ledger.register_realized_pnl(10, reference_id="pnl", ledger_file=self.ledger_file)
        capital_ledger.register_commission(2, reference_id="fee", ledger_file=self.ledger_file)
        capital_ledger.register_funding_fee(1, reference_id="funding", ledger_file=self.ledger_file)
        summary = capital_accounting.get_accounting_summary(111, ledger_file=self.ledger_file)
        self.assertEqual(summary["realized_pnl_net_of_fees"], 10)
        self.assertEqual(summary["trading_fees_informational"], 2)
        self.assertEqual(summary["funding_net"], 1)
        self.assertEqual(summary["trading_pnl_net"], 11)
        self.assertEqual(summary["trading_roi_pct"], 11)

    def test_negative_funding_reduces_net_pnl_once(self):
        capital_ledger.record_movement(capital_ledger.TYPE_INITIAL_CAPITAL, 100, reference_id="initial", ledger_file=self.ledger_file)
        capital_ledger.register_realized_pnl(10, reference_id="pnl", ledger_file=self.ledger_file)
        capital_ledger.register_funding_fee(-1.5, reference_id="funding", ledger_file=self.ledger_file)
        summary = capital_accounting.get_accounting_summary(108.5, ledger_file=self.ledger_file)
        self.assertEqual(summary["trading_pnl_net"], 8.5)
        self.assertEqual(summary["trading_roi_pct"], 8.5)


    def test_unrealized_pnl_and_noise_are_not_deposits(self):
        explained = capital_accounting.classify_observed_capital_change(5, unrealized_pnl_change=5, reference_capital=100)
        noise = capital_accounting.classify_observed_capital_change(0.1, unrealized_pnl_change=0, reference_capital=100)
        unknown = capital_accounting.classify_observed_capital_change(10, unrealized_pnl_change=0, reference_capital=100)
        self.assertEqual(explained["classification"], "NO_MATERIAL_FLOW")
        self.assertEqual(noise["classification"], "NO_MATERIAL_FLOW")
        self.assertEqual(unknown["classification"], "UNKNOWN_CAPITAL_FLOW")


if __name__ == '__main__':
    unittest.main()
