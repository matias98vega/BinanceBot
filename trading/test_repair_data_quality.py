#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import repair_data_quality
import audit_data_quality


class RepairDataQualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        os.makedirs(os.path.join(self.project, 'trading'), exist_ok=True)
        os.makedirs(os.path.join(self.project, 'data', 'history'), exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write_json(self, relpath, payload):
        path = os.path.join(self.project, *relpath.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(payload, f)
        return path

    def write_jsonl(self, relpath, rows):
        path = os.path.join(self.project, *relpath.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            for row in rows:
                f.write(json.dumps(row) + '\n')
        return path

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

    def test_trade_gap_plan_flags_total_close_for_manual_review(self):
        trades = os.path.join(self.project, 'data', 'history', 'trades.jsonl')
        with open(trades, 'w', encoding='utf-8') as f:
            f.write(json.dumps({
                'event_type': 'TRADE_CLOSE',
                'trade_id': 'short_WLDUSDT_1782763085',
                'symbol': 'WLDUSDT',
                'side': 'SHORT',
                'status': 'CLOSED',
                'closed_at': '2026-07-01T12:00:00Z',
                'exit_price': 1.1,
                'pnl_usdt': 0.5,
            }) + '\n')

        plan = repair_data_quality.build_trade_gap_plan(self.project, 'short_WLDUSDT_1782763085')

        self.assertEqual('trade-gap', plan['plan'])
        self.assertEqual('requires_manual_review', plan['classification'])
        self.assertEqual(0, plan['summary']['exact_open_records'])
        self.assertEqual(1, plan['summary']['exact_close_records'])
        self.assertFalse(plan['write_performed'])
        self.assertTrue(any(item['fields'].get('trade_id') == 'short_WLDUSDT_1782763085' for item in plan['evidence']))

    def test_trade_gap_plan_detects_related_open_candidate(self):
        trades = os.path.join(self.project, 'data', 'history', 'trades.jsonl')
        with open(trades, 'w', encoding='utf-8') as f:
            f.write(json.dumps({
                'event_type': 'TRADE_OPEN',
                'trade_id': 'short_WLDUSDT_recovered_1782763000',
                'symbol': 'WLDUSDT',
                'side': 'SHORT',
                'status': 'OPEN',
                'opened_at': '2026-07-01T11:55:00Z',
                'entry_price': 1.2,
            }) + '\n')
            f.write(json.dumps({
                'event_type': 'TRADE_CLOSE',
                'trade_id': 'short_WLDUSDT_1782763085',
                'symbol': 'WLDUSDT',
                'side': 'SHORT',
                'status': 'CLOSED',
                'closed_at': '2026-07-01T12:00:00Z',
                'exit_price': 1.1,
                'pnl_usdt': 0.5,
            }) + '\n')

        plan = repair_data_quality.build_trade_gap_plan(self.project, 'short_WLDUSDT_1782763085')

        self.assertEqual('related_open_requires_manual_mapping', plan['classification'])
        self.assertEqual(1, plan['summary']['related_open_records'])
        self.assertTrue(any(action['action'] == 'manual_link_to_related_open_candidate' for action in plan['proposed_actions']))
        self.assertFalse(any(action['write_allowed'] for action in plan['proposed_actions']))

    def test_trade_gap_plan_not_found_is_diagnostic_only(self):
        plan = repair_data_quality.build_trade_gap_plan(self.project, 'short_WLDUSDT_1782763085')

        self.assertEqual('not_found', plan['classification'])
        self.assertEqual('WLDUSDT', plan['symbol'])
        self.assertEqual([], plan['evidence'])
        self.assertFalse(plan['write_performed'])

    def test_trade_gap_cli_outputs_preview_for_trade_id(self):
        trades = os.path.join(self.project, 'data', 'history', 'trades.jsonl')
        with open(trades, 'w', encoding='utf-8') as f:
            f.write(json.dumps({
                'event_type': 'TRADE_CLOSE',
                'trade_id': 'short_WLDUSDT_1782763085',
                'symbol': 'WLDUSDT',
                'side': 'SHORT',
                'status': 'CLOSED',
                'closed_at': '2026-07-01T12:00:00Z',
                'exit_price': 1.1,
                'pnl_usdt': 0.5,
            }) + '\n')

        with patch('builtins.print') as mocked_print:
            code = repair_data_quality.main([
                '--project-dir', self.project,
                '--plan', 'trade-gap',
                '--trade-id', 'short_WLDUSDT_1782763085',
            ])

        self.assertEqual(0, code)
        payload = json.loads(mocked_print.call_args.args[0])
        self.assertEqual('trade-gap', payload['plan'])
        self.assertEqual('requires_manual_review', payload['classification'])
        self.assertFalse(payload['write_performed'])

    def write_wld_backfill_fixture(self, source_bot_version=None, source_strategy_version=None):
        analytics = os.path.join(self.project, 'trading', 'trade_analytics.jsonl')
        trades = os.path.join(self.project, 'data', 'history', 'trades.jsonl')
        source_open = {
            'trade_id': 'short_WLDUSDT_1782763085',
            'symbol': 'WLDUSDT',
            'side': 'SHORT',
            'status': 'OPEN',
            'entry_price': 0.4267,
            'entry_time': '2026-06-29T19:58:05Z',
        }
        if source_bot_version is not None:
            source_open['bot_version'] = source_bot_version
        if source_strategy_version is not None:
            source_open['strategy_version'] = source_strategy_version
        with open(analytics, 'w', encoding='utf-8') as f:
            f.write(json.dumps(source_open) + '\n')
        with open(trades, 'w', encoding='utf-8') as f:
            f.write(json.dumps({
                'event_type': 'TRADE_CLOSE',
                'trade_id': 'short_WLDUSDT_1782763085:partial',
                'symbol': 'WLDUSDT',
                'side': 'SHORT',
                'status': 'CLOSED',
                'opened_at': '2026-06-29T19:58:05Z',
                'closed_at': '2026-06-30T00:28:50Z',
                'exit_reason': 'PARTIAL',
                'exit_price': 0.42,
                'pnl_usdt': 0.2340000000000002,
            }) + '\n')
            f.write(json.dumps({
                'event_type': 'TRADE_CLOSE',
                'trade_id': 'short_WLDUSDT_1782763085',
                'symbol': 'WLDUSDT',
                'side': 'SHORT',
                'status': 'CLOSED',
                'opened_at': '2026-06-29T19:58:05Z',
                'closed_at': '2026-06-30T01:36:41Z',
                'exit_reason': 'TP',
                'exit_price': 0.4105,
                'pnl_usdt': 0.43645056000000093,
            }) + '\n')
        return analytics, trades

    def test_trade_open_backfill_plan_uses_exact_analytics_open(self):
        self.write_wld_backfill_fixture()

        plan = repair_data_quality.build_trade_open_backfill_plan(
            self.project,
            'short_WLDUSDT_1782763085',
        )

        self.assertEqual('trade-open-backfill', plan['plan'])
        self.assertEqual(
            'missing_trade_open_in_trades_jsonl_but_exact_open_found_in_trade_analytics',
            plan['classification'],
        )
        self.assertTrue(plan['can_apply'])
        self.assertFalse(plan['write_performed'])
        self.assertEqual(1, plan['source_open_count'])
        self.assertEqual(2, plan['target_close_count'])
        self.assertEqual(1, plan['insert_before_line'])
        proposed = plan['proposed_record']
        self.assertEqual('TRADE_OPEN', proposed['event_type'])
        self.assertEqual('short_WLDUSDT_1782763085', proposed['trade_id'])
        self.assertEqual('2026-06-29T19:58:05Z', proposed['opened_at'])
        self.assertEqual(0.4267, proposed['entry_price'])
        self.assertEqual('v1.0-alpha', proposed['bot_version'])
        self.assertEqual('current', proposed['strategy_version'])
        self.assertEqual('v1', proposed['data_schema_version'])
        self.assertEqual('matched_timestamp_range', proposed['repair_metadata']['inferred_bot_version_reason'])
        self.assertEqual(1, proposed['repair_metadata']['source_line'])
        self.assertIsNone(proposed['pnl_usdt'])

    def test_trade_open_backfill_prefers_historical_timestamp_over_runtime_source_version(self):
        self.write_wld_backfill_fixture(source_bot_version='v1.1-observability-hardening')

        plan = repair_data_quality.build_trade_open_backfill_plan(
            self.project,
            'short_WLDUSDT_1782763085',
        )

        self.assertTrue(plan['can_apply'])
        proposed = plan['proposed_record']
        self.assertEqual('v1.0-alpha', proposed['bot_version'])
        self.assertEqual('v1.1-observability-hardening', proposed['repair_metadata']['source_bot_version'])
        self.assertTrue(proposed['repair_metadata']['source_bot_version_contradicted_by_timestamp'])

    def test_trade_open_backfill_respects_matching_source_version(self):
        self.write_wld_backfill_fixture(source_bot_version='v1.0-alpha', source_strategy_version='legacy-strategy')

        plan = repair_data_quality.build_trade_open_backfill_plan(
            self.project,
            'short_WLDUSDT_1782763085',
        )

        proposed = plan['proposed_record']
        self.assertEqual('v1.0-alpha', proposed['bot_version'])
        self.assertEqual('legacy-strategy', proposed['strategy_version'])
        self.assertNotIn('source_bot_version_contradicted_by_timestamp', proposed['repair_metadata'])

    def test_trade_open_backfill_apply_requires_confirmation(self):
        self.write_wld_backfill_fixture()

        result, code = repair_data_quality.apply_trade_open_backfill(
            self.project,
            'short_WLDUSDT_1782763085',
            confirm_trade_id=None,
        )

        self.assertEqual(2, code)
        self.assertFalse(result['write_performed'])
        self.assertEqual('confirmation_required', result['error'])

    def test_trade_open_backfill_apply_creates_backup_report_and_repairs_audit(self):
        self.write_wld_backfill_fixture()
        self.write_json('trading/bot_state.json', {
            'market': {'regime': 'bearish', 'btc_price': 60000, 'btc_change_4h': -1, 'directional_mode': True},
            'capital': {'spot_real': 1, 'spot_used': 0, 'futures_real': 1, 'futures_used': 0},
            'positions': {'long': {'current': 0, 'max': 1}, 'short': {'current': 0, 'max': 1}},
        })
        self.write_jsonl('trading/decision_snapshots.jsonl', [{'timestamp': '2026-06-29T19:58:05Z'}])

        result, code = repair_data_quality.apply_trade_open_backfill(
            self.project,
            'short_WLDUSDT_1782763085',
            confirm_trade_id='short_WLDUSDT_1782763085',
        )

        self.assertEqual(0, code)
        self.assertTrue(result['write_performed'])
        self.assertIn('before_checksum', result)
        self.assertIn('after_checksum', result)
        self.assertNotEqual(result['before_checksum'], result['after_checksum'])
        self.assertTrue(os.path.exists(os.path.join(self.project, result['backup_path'])))
        self.assertTrue(os.path.exists(os.path.join(self.project, result['report_path'])))

        trades = os.path.join(self.project, 'data', 'history', 'trades.jsonl')
        with open(trades, encoding='utf-8') as f:
            rows = [json.loads(line) for line in f if line.strip()]
        self.assertEqual('TRADE_OPEN', rows[0]['event_type'])
        self.assertEqual('short_WLDUSDT_1782763085', rows[0]['trade_id'])
        self.assertEqual('v1.0-alpha', rows[0]['bot_version'])
        self.assertEqual('v1', rows[0]['data_schema_version'])
        self.assertEqual('TRADE_CLOSE', rows[1]['event_type'])

        report = audit_data_quality.audit_project(self.project)
        self.assertFalse(any('short_WLDUSDT_1782763085' in item for item in report.errors))

    def test_trade_open_backfill_refuses_when_history_open_already_exists(self):
        self.write_wld_backfill_fixture()
        trades = os.path.join(self.project, 'data', 'history', 'trades.jsonl')
        with open(trades, 'r', encoding='utf-8') as f:
            original = f.read()
        with open(trades, 'w', encoding='utf-8') as f:
            f.write(json.dumps({
                'event_type': 'TRADE_OPEN',
                'trade_id': 'short_WLDUSDT_1782763085',
                'symbol': 'WLDUSDT',
                'side': 'SHORT',
                'status': 'OPEN',
                'opened_at': '2026-06-29T19:58:05Z',
                'entry_price': 0.4267,
            }) + '\n')
            f.write(original)

        plan = repair_data_quality.build_trade_open_backfill_plan(
            self.project,
            'short_WLDUSDT_1782763085',
        )

        self.assertEqual('already_repaired', plan['classification'])
        self.assertFalse(plan['can_apply'])

    def test_data_hygiene_backfill_proposes_market_regime_from_regime(self):
        self.write_jsonl('trading/trade_analytics.jsonl', [
            {
                'timestamp': '2026-07-08T01:00:00Z',
                'trade_id': 't1',
                'symbol': 'ETHUSDT',
                'regime': 'bear',
                'capital': {'position_final': 10},
            }
        ])

        plan = repair_data_quality.build_data_hygiene_backfill_plan(self.project)

        self.assertEqual('data-hygiene-backfill', plan['plan'])
        self.assertTrue(any(
            item['field'] == 'market.regime' and item['source_field'] == 'regime' and item['value'] == 'bear'
            for item in plan['proposed_changes']
        ))
        self.assertFalse(plan['write_performed'])

    def test_data_hygiene_backfill_proposes_market_regime_from_market_regime(self):
        self.write_jsonl('trading/trade_analytics.jsonl', [
            {
                'timestamp': '2026-07-08T01:00:00Z',
                'trade_id': 't1',
                'symbol': 'ETHUSDT',
                'market_regime': 'bull',
                'capital': {'position_final': 10},
            }
        ])

        plan = repair_data_quality.build_data_hygiene_backfill_plan(self.project)

        self.assertTrue(any(
            item['field'] == 'market.regime' and item['source_field'] == 'market_regime' and item['value'] == 'bull'
            for item in plan['proposed_changes']
        ))

    def test_data_hygiene_backfill_does_not_invent_position_final(self):
        self.write_jsonl('trading/trade_analytics.jsonl', [
            {
                'timestamp': '2026-07-08T01:00:00Z',
                'trade_id': 't1',
                'symbol': 'ETHUSDT',
                'market': {'regime': 'bear'},
                'capital': {'spot': 10},
            }
        ])

        plan = repair_data_quality.build_data_hygiene_backfill_plan(self.project)

        self.assertFalse(any(item['field'] == 'capital.position_final' for item in plan['proposed_changes']))
        self.assertTrue(any(item['field'] == 'capital.position_final' for item in plan['optional_unresolved']))

    def test_data_hygiene_backfill_proposes_inferable_bot_version(self):
        self.write_jsonl('trading/trade_analytics.jsonl', [
            {
                'timestamp': '2026-07-08T01:00:00Z',
                'trade_id': 't1',
                'symbol': 'ETHUSDT',
                'market': {'regime': 'bear'},
                'capital': {'position_final': 10},
            }
        ])

        plan = repair_data_quality.build_data_hygiene_backfill_plan(self.project)

        self.assertTrue(any(
            item['field'] == 'bot_version' and item['value'] == 'v1.1-observability-hardening'
            for item in plan['proposed_changes']
        ))

    def test_data_hygiene_backfill_keeps_uninferable_bot_version_unresolved(self):
        self.write_jsonl('trading/trade_analytics.jsonl', [
            {
                'trade_id': 't1',
                'symbol': 'ETHUSDT',
                'market': {'regime': 'bear'},
                'capital': {'position_final': 10},
            }
        ])

        plan = repair_data_quality.build_data_hygiene_backfill_plan(self.project)

        self.assertFalse(any(item['field'] == 'bot_version' for item in plan['proposed_changes']))
        self.assertTrue(any(item['field'] == 'bot_version' for item in plan['optional_unresolved']))

    def test_suspicious_test_record_plan_reports_t1_without_writing(self):
        self.write_jsonl('trading/trade_analytics.jsonl', [
            {
                'timestamp': '2026-07-08T01:00:00Z',
                'event_type': 'TRADE_OPEN',
                'trade_id': 't1',
                'symbol': 'ETHUSDT',
                'side': 'LONG',
                'status': 'OPEN',
                'entry_price': 10,
            }
        ])

        plan = repair_data_quality.build_suspicious_test_record_plan(self.project, trade_id='t1')

        self.assertEqual('suspicious-test-record', plan['plan'])
        self.assertEqual('suspicious_test_record_without_state_evidence', plan['classification'])
        self.assertEqual(1, plan['match_count'])
        self.assertFalse(plan['write_performed'])


if __name__ == '__main__':
    unittest.main()
