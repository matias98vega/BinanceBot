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

import analyze_version_performance


class AnalyzeVersionPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.trades = os.path.join(self.tmp.name, 'trades.jsonl')
        rows = [
            {'event_type': 'TRADE_OPEN', 'trade_id': 't1', 'status': 'OPEN', 'symbol': 'AAA', 'side': 'LONG', 'opened_at': '2026-01-01T00:00:00Z', 'bot_version': 'v-test', 'regime': 'bull', 'capital_used': 10},
            {'event_type': 'TRADE_CLOSE', 'trade_id': 't1', 'status': 'CLOSED', 'symbol': 'AAA', 'side': 'LONG', 'closed_at': '2026-01-01T01:00:00Z', 'exit_reason': 'TP', 'pnl_usdt': 2},
        ]
        with open(self.trades, 'w', encoding='utf-8') as file:
            for row in rows:
                file.write(json.dumps(row) + '\n')

    def tearDown(self):
        self.tmp.cleanup()

    def _hash(self):
        with open(self.trades, 'rb') as file:
            return hashlib.sha256(file.read()).hexdigest()

    def test_json_report_uses_requested_version_and_is_read_only(self):
        before = self._hash()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = analyze_version_performance.main([
                '--version', 'v-test', '--json', '--trades-file', self.trades,
            ])
        report = json.loads(output.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(report['version'], 'v-test')
        self.assertEqual(report['summary']['closed'], 1)
        self.assertEqual(before, self._hash())

    def test_text_report_defaults_to_success_for_empty_version(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = analyze_version_performance.main([
                '--version', 'missing', '--trades-file', self.trades,
            ])

        self.assertEqual(code, 0)
        self.assertIn('BOT VERSION PERFORMANCE: missing', output.getvalue())
        self.assertIn('Trades: 0', output.getvalue())

    def test_flags_are_explicit(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            analyze_version_performance.main([
                '--version', 'v-test', '--json', '--trades-file', self.trades,
            ])
        report = json.loads(output.getvalue())

        self.assertIn('LOW_SAMPLE', report['flags'])
        self.assertEqual(report['flag_rules']['LOW_SAMPLE'], 'closed trades < 30')
        self.assertNotIn('NEGATIVE_EXPECTANCY', report['flags'])


if __name__ == '__main__':
    unittest.main()
