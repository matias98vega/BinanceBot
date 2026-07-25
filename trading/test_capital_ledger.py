#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import analytics_engine
import capital_ledger


class CapitalLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger_file = os.path.join(self.tmp.name, 'capital_ledger.jsonl')

    def tearDown(self):
        self.tmp.cleanup()

    def _lines(self):
        with open(self.ledger_file, encoding='utf-8') as f:
            return [line.strip() for line in f if line.strip()]

    def test_register_external_deposit_creates_append_only_jsonl(self):
        first = capital_ledger.register_external_deposit(
            100,
            description='manual deposit',
            reference_id='dep-1',
            metadata={'note': 'ok', 'api_secret': 'hidden'},
            timestamp='2026-07-02T10:00:00Z',
            ledger_file=self.ledger_file,
        )
        second = capital_ledger.register_external_deposit(25, ledger_file=self.ledger_file)

        lines = self._lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(first['type'], 'external_deposit')
        self.assertEqual(second['amount'], 25.0)
        stored = json.loads(lines[0])
        self.assertEqual(stored['description'], 'manual deposit')
        self.assertEqual(stored['reference_id'], 'dep-1')
        self.assertNotIn('api_secret', stored['metadata'])

    def test_read_history_filters_and_ignores_corrupt_lines(self):
        capital_ledger.register_external_deposit(100, ledger_file=self.ledger_file)
        with open(self.ledger_file, 'a', encoding='utf-8') as f:
            f.write('{invalid json\n')
        capital_ledger.register_external_withdrawal(30, ledger_file=self.ledger_file)

        with self.assertLogs(level='WARNING') as logs:
            all_records = capital_ledger.read_history(ledger_file=self.ledger_file)

        withdrawals = capital_ledger.read_history(ledger_file=self.ledger_file, movement_type='external_withdrawal')
        self.assertEqual(len(all_records), 2)
        self.assertEqual(len(withdrawals), 1)
        self.assertEqual(withdrawals[0]['amount'], 30.0)
        self.assertTrue(any('capital ledger JSONL invalid' in line for line in logs.output))

    def test_all_required_registration_helpers(self):
        capital_ledger.register_external_deposit(100, ledger_file=self.ledger_file)
        capital_ledger.register_external_withdrawal(20, ledger_file=self.ledger_file)
        capital_ledger.register_rebalance(15, metadata={'direction': 'SPOT_TO_FUTURES'}, ledger_file=self.ledger_file)
        capital_ledger.register_commission(0.1, ledger_file=self.ledger_file)
        capital_ledger.register_funding_fee(-0.03, ledger_file=self.ledger_file)
        capital_ledger.register_realized_pnl(4.5, reference_id='trade-1', ledger_file=self.ledger_file)

        records = capital_ledger.read_history(ledger_file=self.ledger_file)
        self.assertEqual([record['type'] for record in records], [
            'external_deposit',
            'external_withdrawal',
            'rebalance',
            'commission',
            'funding_fee',
            'realized_pnl',
        ])

    def test_totals_net_deposits_withdrawals_and_adjusted_pnl(self):
        capital_ledger.register_external_deposit(100, ledger_file=self.ledger_file)
        capital_ledger.register_external_deposit(50, ledger_file=self.ledger_file)
        capital_ledger.register_external_withdrawal(30, ledger_file=self.ledger_file)
        capital_ledger.register_commission(0.2, ledger_file=self.ledger_file)

        totals = capital_ledger.get_totals_by_type(ledger_file=self.ledger_file)
        summary = capital_ledger.get_external_capital_summary(ledger_file=self.ledger_file)

        self.assertEqual(totals['external_deposit'], 150.0)
        self.assertEqual(totals['external_withdrawal'], 30.0)
        self.assertEqual(totals['commission'], 0.2)
        self.assertEqual(capital_ledger.get_net_deposits(ledger_file=self.ledger_file), 150.0)
        self.assertEqual(capital_ledger.get_net_withdrawals(ledger_file=self.ledger_file), 30.0)
        self.assertEqual(summary['external_net'], 120.0)
        self.assertEqual(capital_ledger.estimate_adjusted_pnl(200, ledger_file=self.ledger_file), 80.0)

    def test_missing_file_returns_empty_results(self):
        self.assertEqual(capital_ledger.read_history(ledger_file=self.ledger_file), [])
        self.assertEqual(capital_ledger.get_totals_by_type(ledger_file=self.ledger_file), {})
        self.assertEqual(capital_ledger.get_net_deposits(ledger_file=self.ledger_file), 0.0)
        self.assertEqual(capital_ledger.get_net_withdrawals(ledger_file=self.ledger_file), 0.0)

    def test_asset_filter(self):
        capital_ledger.register_external_deposit(100, asset='USDT', ledger_file=self.ledger_file)
        capital_ledger.register_external_deposit(2, asset='BTC', ledger_file=self.ledger_file)

        self.assertEqual(capital_ledger.get_net_deposits(ledger_file=self.ledger_file, asset='USDT'), 100.0)
        self.assertEqual(capital_ledger.get_net_deposits(ledger_file=self.ledger_file, asset='BTC'), 2.0)

    def test_unknown_future_type_is_supported(self):
        record = capital_ledger.record_movement('manual_adjustment', 7, ledger_file=self.ledger_file)

        self.assertEqual(record['type'], 'manual_adjustment')
        self.assertEqual(capital_ledger.get_totals_by_type(ledger_file=self.ledger_file)['manual_adjustment'], 7.0)

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
        capital_ledger.register_external_deposit(1000, ledger_file=self.ledger_file)
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

    def test_write_failure_does_not_raise(self):
        with patch('builtins.open', side_effect=OSError('no write')):
            record = capital_ledger.register_external_deposit(100, ledger_file=self.ledger_file)

        self.assertEqual(record['type'], 'external_deposit')


    def test_event_id_is_deterministic_and_append_is_idempotent(self):
        first = capital_ledger.register_external_deposit(10, reference_id="binance-1", timestamp="2026-07-20T12:00:00Z", ledger_file=self.ledger_file)
        second = capital_ledger.register_external_deposit(10, reference_id="binance-1", timestamp="2026-07-20T12:00:00Z", ledger_file=self.ledger_file)
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(self._lines()), 1)
        self.assertEqual(first["accounting_convention"], "realized_pnl_net_of_fees_plus_signed_funding")

    def test_corrective_close_is_idempotent_net_and_does_not_change_trade_counts(self):
        kwargs = dict(
            gross_realized_pnl='0.02760000', trading_fee='0.00212108',
            net_realized_pnl='0.02547892', symbol='AMDUSDT', side='SHORT',
            quantity='0.01', order_id='352126125',
            client_order_id='amd-cleanup-1784951245-230ccfd',
            exchange_trade_id='16670048', original_trade_id='short_AMD_1',
            reason='precision_quantity_split_mismatch', timestamp='2026-07-25T20:29:47Z',
            idempotency_key='amd-cleanup-352126125-16670048',
            bot_version='v1.2-sizing-v2', position_zero_confirmed=True,
            ledger_file=self.ledger_file)
        before = analytics_engine._empty_stats()['general']
        first = capital_ledger.register_corrective_close(**kwargs)
        second = capital_ledger.register_corrective_close(**kwargs)
        totals = capital_ledger.get_totals_by_type(ledger_file=self.ledger_file)

        self.assertFalse(first['already_recorded'])
        self.assertTrue(second['already_recorded'])
        self.assertEqual(len(self._lines()), 2)
        self.assertEqual(totals['realized_pnl'], 0.02547892)
        self.assertEqual(totals['commission'], 0.00212108)
        self.assertEqual(before['trades'], 0)
        self.assertEqual(before['win_rate'], 0.0)

    def test_corrective_close_rejects_double_fee_subtraction(self):
        with self.assertRaisesRegex(ValueError, 'gross_realized_pnl - trading_fee'):
            capital_ledger.register_corrective_close(
                gross_realized_pnl='0.02760000', trading_fee='0.00212108',
                net_realized_pnl='0.02335784', symbol='AMDUSDT', side='SHORT',
                quantity='0.01', order_id='1', client_order_id='c1',
                exchange_trade_id='2', original_trade_id='t1', reason='x',
                timestamp='2026-07-25T20:29:47Z', idempotency_key='k1',
                bot_version='v1.2-sizing-v2', position_zero_confirmed=True,
                ledger_file=self.ledger_file)


if __name__ == '__main__':
    unittest.main()
