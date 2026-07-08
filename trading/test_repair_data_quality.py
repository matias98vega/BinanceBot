#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import repair_data_quality


class RepairDataQualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        os.makedirs(os.path.join(self.project, 'trading'), exist_ok=True)
        os.makedirs(os.path.join(self.project, 'data', 'history'), exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_repair_plan_is_dry_run_only(self):
        plan = repair_data_quality.build_repair_plan(self.project)

        self.assertEqual('dry_run', plan['mode'])
        self.assertFalse(plan['write_performed'])
        self.assertIn('available_repair_types', plan)

    def test_partial_false_positive_becomes_review_candidate(self):
        class FakeReport:
            files_checked = 1
            records_checked = 1
            errors = []
            warnings = []
            recommendations = {'requires review'}
            possible_false_positives = [{
                'message': 'cierre parcial sin apertura exacta pero base existe',
                'path': 'data/history/trades.jsonl',
                'line': 12,
                'trade_id': 'short_TEST_1:partial',
                'symbol': 'TESTUSDT',
            }]

        with patch.object(repair_data_quality.audit_data_quality, 'audit_project', return_value=FakeReport()):
            plan = repair_data_quality.build_repair_plan(self.project)

        self.assertEqual(1, len(plan['candidates']))
        self.assertEqual('partial_close_base_trade_id', plan['candidates'][0]['issue_type'])
        self.assertFalse(plan['candidates'][0]['write_allowed'])

    def test_write_mode_is_rejected(self):
        self.assertEqual(2, repair_data_quality.main(['--project-dir', self.project, '--write']))

    def test_apply_mode_is_rejected(self):
        self.assertEqual(2, repair_data_quality.main(['--project-dir', self.project, '--apply']))

    def test_cli_outputs_json_plan(self):
        with patch('builtins.print') as mocked_print:
            code = repair_data_quality.main(['--project-dir', self.project])

        self.assertEqual(0, code)
        printed = mocked_print.call_args.args[0]
        payload = json.loads(printed)
        self.assertEqual('dry_run', payload['mode'])

    def test_version_backfill_plan_detects_missing_version(self):
        trades = os.path.join(self.project, 'data', 'history', 'trades.jsonl')
        with open(trades, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'trade_id': 't1', 'recorded_at': '2026-07-08T12:00:00Z'}) + '\n')

        plan = repair_data_quality.build_version_backfill_plan(self.project)

        self.assertEqual('version-backfill', plan['plan'])
        self.assertEqual(1, plan['records_without_version'])
        self.assertEqual(1, plan['records_classifiable'])
        self.assertEqual({'v1.1-observability-hardening': 1}, plan['suggested_versions'])
        self.assertFalse(plan['write_performed'])

    def test_version_backfill_cli_outputs_preview(self):
        trades = os.path.join(self.project, 'data', 'history', 'trades.jsonl')
        with open(trades, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'trade_id': 't1'}) + '\n')

        with patch('builtins.print') as mocked_print:
            code = repair_data_quality.main(['--project-dir', self.project, '--plan', 'version-backfill'])

        self.assertEqual(0, code)
        payload = json.loads(mocked_print.call_args.args[0])
        self.assertEqual(1, payload['records_without_version'])
        self.assertEqual(1, payload['records_unclassifiable'])
        self.assertFalse(payload['write_performed'])


if __name__ == '__main__':
    unittest.main()
