#!/usr/bin/env python3
import os
import sys
import tempfile
import unittest
import json
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import analytics
import bot_state
import decision_timeline
import feature_store
import futures_reconciliation
import history
import residuals
import version_history


class VersionHistoryTests(unittest.TestCase):
    def test_current_version_reads_version_file(self):
        self.assertEqual('v1.2-sizing-v2', version_history.current_version())

    def test_history_exposes_schema_current_and_versions(self):
        payload = version_history.get_version_history()

        self.assertEqual(1, payload['schema_version'])
        self.assertEqual('v1.2-sizing-v2', payload['current_version'])
        self.assertTrue(payload['versions'])

    def test_current_version_metadata(self):
        self.assertEqual(
            {
                'bot_version': 'v1.2-sizing-v2',
                'strategy_version': 'current',
                'data_schema_version': 'v1',
            },
            version_history.get_current_version_metadata(),
        )

    def test_attach_version_metadata_adds_fields(self):
        record = {'trade_id': 't1'}

        result = version_history.attach_version_metadata(record)

        self.assertIs(result, record)
        self.assertEqual('v1.2-sizing-v2', record['bot_version'])
        self.assertEqual('current', record['strategy_version'])
        self.assertEqual('v1', record['data_schema_version'])
        self.assertTrue(version_history.has_top_level_version_metadata(record))

    def test_attach_version_metadata_does_not_overwrite_by_default(self):
        record = {'bot_version': 'old', 'strategy_version': 'legacy', 'data_schema_version': 'old_schema'}

        version_history.attach_version_metadata(record)

        self.assertEqual('old', record['bot_version'])
        self.assertEqual('legacy', record['strategy_version'])
        self.assertEqual('old_schema', record['data_schema_version'])

    def test_attach_version_metadata_overwrites_when_requested(self):
        record = {'bot_version': 'old', 'strategy_version': 'legacy', 'data_schema_version': 'old_schema'}

        version_history.attach_version_metadata(record, overwrite=True)

        self.assertEqual('v1.2-sizing-v2', record['bot_version'])
        self.assertEqual('current', record['strategy_version'])
        self.assertEqual('v1', record['data_schema_version'])

    def test_timestamp_classification_matches_alpha_range(self):
        record = {
            'recorded_at': '2026-07-07T12:00:00Z',
            'trade_id': 'short_TEST_1',
        }

        classified = version_history.classify_record(record)

        self.assertEqual('v1.0-alpha', classified['version'])
        self.assertEqual('usable_with_audit_flags', classified['reliability'])
        self.assertEqual('matched_timestamp_range', classified['reason'])

    def test_timestamp_classification_uses_entry_time_alias(self):
        classified = version_history.classify_record({'entry_time': '2026-06-08T19:55:19Z'})

        self.assertEqual('v1.0-alpha', classified['version'])

    def test_explicit_version_takes_precedence(self):
        record = {
            'bot_version': 'v1.0-alpha',
            'recorded_at': '2025-01-01T00:00:00Z',
        }

        classified = version_history.classify_record(record)

        self.assertEqual('v1.0-alpha', classified['version'])
        self.assertEqual('matched_explicit_version', classified['reason'])

    def test_unknown_record_degrades_without_exception(self):
        classified = version_history.classify_record({'trade_id': 'missing_time'})

        self.assertEqual('unknown', classified['version'])
        self.assertEqual('unknown', classified['reliability'])

    def test_bot_state_new_payload_includes_metadata(self):
        with patch.object(bot_state, 'get_system_statuses', return_value={'bot': 'UNKNOWN', 'guardian': 'UNKNOWN', 'dashboard': 'UNKNOWN'}):
            payload = bot_state.build_bot_state(state={'positions': []}, btc_ctx={'trend': 'neutral'})

        self.assertEqual('v1.2-sizing-v2', payload['bot_version'])
        self.assertEqual('current', payload['strategy_version'])
        self.assertEqual('v1', payload['data_schema_version'])

    def test_bot_state_writer_persists_top_level_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'bot_state.json')
            payload = {'timestamp': '2026-07-08T00:00:00Z'}
            with patch.object(bot_state, 'BOT_STATE_FILE', path):
                bot_state.persist_bot_state(payload)
            with open(path, encoding='utf-8') as f:
                data = json.load(f)

        self.assertEqual('v1.2-sizing-v2', data.get('bot_version'))
        self.assertEqual('current', data.get('strategy_version'))
        self.assertEqual('v1', data.get('data_schema_version'))
        self.assertTrue(version_history.has_top_level_version_metadata(data))

    def test_timeline_record_includes_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'timeline.jsonl')
            record = decision_timeline.record_event('test', 'message', path=path)
            with open(path, encoding='utf-8') as f:
                persisted = json.loads(f.readline())

        self.assertEqual('v1.2-sizing-v2', record['bot_version'])
        self.assertEqual('current', record['strategy_version'])
        self.assertEqual('v1', record['data_schema_version'])
        self.assertEqual('v1.2-sizing-v2', persisted.get('bot_version'))
        self.assertTrue(version_history.has_top_level_version_metadata(persisted))

    def test_history_trade_records_include_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = history.HistoryStore(
                trades_file=os.path.join(tmp, 'trades.jsonl'),
                decisions_file=os.path.join(tmp, 'decisions.jsonl'),
                snapshots_file=os.path.join(tmp, 'snapshots.jsonl'),
            )
            with patch.object(history.decision_timeline, 'record_event'):
                record = store.record_trade_open('t1', 'ETHUSDT', 'LONG')
            with open(store.trades_file, encoding='utf-8') as f:
                persisted = json.loads(f.readline())

        self.assertEqual('v1.2-sizing-v2', record['bot_version'])
        self.assertEqual('current', record['strategy_version'])
        self.assertEqual('v1', record['data_schema_version'])
        self.assertEqual('v1.2-sizing-v2', persisted.get('bot_version'))
        self.assertTrue(version_history.has_top_level_version_metadata(persisted))

    def test_analytics_trade_record_includes_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = analytics.AnalyticsLogger(path=os.path.join(tmp, 'trade_analytics.jsonl'))
            with patch.object(analytics.decision_timeline, 'record_event'), \
                 patch.object(analytics.history, 'record_trade_open'), \
                 patch.object(analytics.history, 'record_snapshot'), \
                 patch.object(analytics.feature_store, 'record_trade_features'):
                record = logger.log_trade_open('t1', 'ETHUSDT', 'LONG', 100)
            with open(logger.path, encoding='utf-8') as f:
                persisted = json.loads(f.readline())

        self.assertEqual('v1.2-sizing-v2', record['bot_version'])
        self.assertEqual('current', record['strategy_version'])
        self.assertEqual('v1', record['data_schema_version'])
        self.assertEqual('v1.2-sizing-v2', persisted.get('bot_version'))
        self.assertTrue(version_history.has_top_level_version_metadata(persisted))

    def test_feature_record_includes_metadata(self):
        record = feature_store._record_from_kwargs({'trade_id': 't1', 'symbol': 'ETHUSDT', 'side': 'LONG'})

        self.assertEqual('v1.2-sizing-v2', record['bot_version'])
        self.assertEqual('v1.2-sizing-v2', record['identification']['bot_version'])
        self.assertEqual('v1', record['data_schema_version'])

    def test_feature_writer_persists_top_level_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'features.jsonl')
            record = feature_store.record_trade_features(
                features_file=path,
                trade_id='t1',
                symbol='ETHUSDT',
                side='LONG',
            )
            with open(path, encoding='utf-8') as f:
                persisted = json.loads(f.readline())

        self.assertEqual('v1.2-sizing-v2', record['bot_version'])
        self.assertEqual('v1.2-sizing-v2', persisted.get('bot_version'))
        self.assertTrue(version_history.has_top_level_version_metadata(persisted))

    def test_futures_reconciliation_status_includes_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'futures_reconciliation_status.json')
            payload = futures_reconciliation.persist_reconciliation({}, status_file=path)
            with open(path, encoding='utf-8') as f:
                data = json.load(f)

        self.assertEqual('v1.2-sizing-v2', payload['bot_version'])
        self.assertEqual('current', payload['strategy_version'])
        self.assertEqual('v1', payload['data_schema_version'])
        self.assertEqual('v1.2-sizing-v2', data.get('bot_version'))
        self.assertEqual('current', data.get('strategy_version'))
        self.assertEqual('v1', data.get('data_schema_version'))
        self.assertTrue(version_history.has_top_level_version_metadata(data))

    def test_residual_status_includes_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'residuals_status.json')
            residuals.classify_unprotectable_residual('SOLUSDT', 'SOL', 0.1, 1.0, 5.0, path=path)
            data = residuals.load_status(path)

        self.assertEqual('v1.2-sizing-v2', data['bot_version'])
        self.assertEqual('current', data['strategy_version'])
        self.assertEqual('v1', data['data_schema_version'])
        self.assertTrue(version_history.has_top_level_version_metadata(data))


if __name__ == '__main__':
    unittest.main()
