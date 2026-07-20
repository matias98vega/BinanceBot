#!/usr/bin/env python3
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))

import analyze_short_performance
import short_performance


class ShortPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.trades = os.path.join(self.tmp.name, 'trades.jsonl')
        self.features = os.path.join(self.tmp.name, 'features.jsonl')
        trades = []
        specs = [
            ('a', 'AAA', 'bear', 'TP', 2.0, 42, -1.0, 1.2),
            ('b', 'BBB', 'neutral', 'SL', -3.0, 55, 0.5, 0.7),
            ('c', 'CCC', 'bearish', 'STALE', -1.0, 52, 0.2, 0.8),
            ('d', 'AAA', 'bear', 'SL', -2.0, 48, -0.4, 1.1),
            ('e', 'DDD', 'neutral', 'TP', 1.0, 45, -0.8, 1.5),
            ('f', 'EEE', 'bear', 'SL', -1.0, 50, 0.1, 0.6),
        ]
        feature_rows = []
        for index, (trade_id, symbol, regime, reason, pnl, rsi, btc4h, volume) in enumerate(specs):
            opened = f'2026-01-0{index + 1}T0{index}:00:00Z'
            trades.extend([
                {'event_type': 'TRADE_OPEN', 'trade_id': trade_id, 'status': 'OPEN', 'symbol': symbol, 'side': 'SHORT', 'opened_at': opened, 'bot_version': 'v-test', 'regime': regime, 'capital_used': 10 + index},
                {'event_type': 'TRADE_CLOSE', 'trade_id': trade_id, 'status': 'CLOSED', 'symbol': symbol, 'side': 'SHORT', 'closed_at': f'2026-01-0{index + 1}T0{index}:30:00Z', 'bot_version': 'later', 'exit_reason': reason, 'pnl_usdt': pnl, 'pnl_pct': pnl, 'duration_minutes': 30 + index},
            ])
            feature_rows.append({
                'identification': {'trade_id': trade_id, 'bot_version': 'v-test'},
                'market': {'regime': regime, 'btc_change_4h': btc4h, 'btc_price': 60000 + index, 'atr': 1 + index, 'volatility': 1, 'volume_ratio': volume, 'hour_utc': index, 'weekday': index},
                'symbol_indicators': {'entry_price': 100, 'ema20': 101, 'ema50': 102, 'rsi': rsi, 'macd_hist': -0.1 + index / 20, 'distance_to_ema20_pct': -1, 'distance_to_ema50_pct': -2},
                'scoring': {'score_total': 8 + index},
                'capital': {'position_final': 10 + index, 'leverage': 2},
                'extra': {'btc_correlation': 0},
            })
        trades.append({'event_type': 'TRADE_OPEN', 'trade_id': 'long', 'status': 'OPEN', 'symbol': 'LONG', 'side': 'LONG', 'bot_version': 'v-test'})
        self._write(self.trades, trades)
        self._write(self.features, feature_rows)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, path, rows):
        with open(path, 'w', encoding='utf-8') as file:
            for row in rows:
                file.write(json.dumps(row) + '\n')

    def _report(self, min_sample=3):
        return short_performance.build_report('v-test', min_sample, 10, self.trades, self.features)

    def test_filters_short_and_preserves_opening_version(self):
        report = self._report()
        self.assertEqual(report['universe']['total'], 6)
        self.assertEqual((report['universe']['winners'], report['universe']['losers']), (2, 4))
        self.assertNotIn('LONG', [item['symbol'] for item in report['symbols']['worst']])

    def test_regime_exit_bands_missingness_and_concentration(self):
        report = self._report()
        self.assertEqual(report['regimes']['NEUTRAL']['closed'], 2)
        self.assertEqual(report['regimes']['BEAR']['closed'], 4)
        self.assertEqual(report['exit_reasons']['PREVENTIVE']['closed'], 1)
        self.assertEqual(report['exit_reasons']['SL']['closed'], 3)
        self.assertEqual(report['bands']['rsi']['status'], 'OK')
        self.assertEqual(report['bands']['rsi']['valid'], 6)
        self.assertEqual(report['data_quality']['opening_snapshot_missing'], 0)
        self.assertEqual(report['data_quality']['missingness']['btc_correlation']['missing'], 0)
        self.assertGreater(report['symbols']['concentration']['top1_loss_percent'], 0)

    def test_comparison_bootstrap_flags_and_candidate_rules(self):
        report = self._report()
        self.assertEqual(report['winner_loser_comparison']['rsi']['valid'], 6)
        self.assertIsNotNone(report['winner_loser_comparison']['rsi']['mean_difference'])
        self.assertEqual(report['bootstrap']['pnl_mean_95_ci']['seed'], 42)
        self.assertIn('SHORT_NEGATIVE_EXPECTANCY', report['flags'])
        self.assertIn('SHORT_PROFIT_FACTOR_BELOW_1', report['flags'])
        self.assertIn('SHORT_SL_DRAG', report['flags'])
        self.assertIn('LOW_SAMPLE', report['flags'])
        self.assertIn('WALK_FORWARD_INSUFFICIENT_SAMPLE', report['flags'])
        self.assertEqual(len(report['candidate_rules']), 4)
        self.assertTrue(all(rule['exploratory_only'] for rule in report['candidate_rules']))

    def test_insufficient_bands_preventive_and_sl_details(self):
        report = self._report(min_sample=20)
        self.assertEqual(report['bands']['rsi']['status'], 'INSUFFICIENT_SAMPLE')
        self.assertEqual(report['preventive_closes']['summary']['closed'], 1)
        self.assertEqual(report['sl_closes']['summary']['closed'], 3)
        self.assertIn('No post-entry counterfactual', report['preventive_closes']['limitation'])

    def test_cli_text_json_and_read_only(self):
        with open(self.trades, 'rb') as file:
            before = hashlib.sha256(file.read()).hexdigest()
        text = io.StringIO()
        with contextlib.redirect_stdout(text):
            code = analyze_short_performance.main(['--version', 'v-test', '--min-sample', '3', '--trades-file', self.trades, '--features-file', self.features])
        self.assertEqual(code, 0)
        self.assertIn('EXPLORATORY / NOT CAUSAL', text.getvalue())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = analyze_short_performance.main(['--version', 'v-test', '--json', '--trades-file', self.trades, '--features-file', self.features])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())['version'], 'v-test')
        with open(self.trades, 'rb') as file:
            after = hashlib.sha256(file.read()).hexdigest()
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
