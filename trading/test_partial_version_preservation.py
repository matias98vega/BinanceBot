import json
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(__file__))

from analytics import AnalyticsLogger
from check_version_consistency import validate
from history import HistoryStore, resolve_derived_trade_version


V12 = 'v1.2-sizing-v2'
V13 = 'v1.3-partial-quantity-fix'


class DerivedVersionPreservationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.analytics_path = os.path.join(self.tempdir.name, 'analytics.jsonl')
        self.trades_path = os.path.join(self.tempdir.name, 'trades.jsonl')
        self.store = HistoryStore(
            trades_file=self.trades_path,
            decisions_file=os.path.join(self.tempdir.name, 'decisions.jsonl'),
            snapshots_file=os.path.join(self.tempdir.name, 'snapshots.jsonl'),
            timeline_recorder=Mock(),
        )
        self.logger = AnalyticsLogger(
            path=self.analytics_path,
            history_store=self.store,
            timeline_recorder=Mock(),
        )
        feature_patch = patch('analytics.feature_store.record_trade_features')
        feature_patch.start()
        self.addCleanup(feature_patch.stop)

    @staticmethod
    def _rows(path):
        with open(path, encoding='utf-8') as stream:
            return [json.loads(line) for line in stream if line.strip()]

    def _open(self, trade_id, version=V12, symbol='AMDUSDT'):
        return self.logger.log_trade_open(
            trade_id=trade_id,
            symbol=symbol,
            side='SHORT',
            entry_price=100,
            bot_version=version,
            entry_time='2026-07-01T00:00:00Z',
        )

    def _close(self, trade_id, reason='TP', **extra):
        return self.logger.log_trade_close(
            trade_id=trade_id,
            symbol=extra.pop('symbol', 'AMDUSDT'),
            side='SHORT',
            entry_price=100,
            entry_time='2026-07-01T00:00:00Z',
            exit_price=90,
            exit_time='2026-07-01T01:00:00Z',
            exit_reason=reason,
            pnl_usdt=0.1,
            **extra,
        )

    def assert_all_event_versions(self, trade_id, expected):
        base_id = trade_id.removesuffix(':partial')
        for path in (self.analytics_path, self.trades_path):
            relevant = [
                row for row in self._rows(path)
                if str(row.get('trade_id') or '').removesuffix(':partial') == base_id
            ]
            self.assertTrue(relevant, path)
            self.assertEqual([expected] * len(relevant), [row.get('bot_version') for row in relevant])

    def test_tp_and_sl_closed_after_upgrade_keep_v12(self):
        for suffix, reason in (('tp', 'TP'), ('sl', 'SL')):
            trade_id = f'short_AMDUSDT_{suffix}'
            self._open(trade_id, V12)
            close = self._close(trade_id, reason)
            self.assertEqual(V12, close['bot_version'])
            self.assert_all_event_versions(trade_id, V12)

    def test_partial_and_final_close_keep_opening_version(self):
        trade_id = 'short_AMDUSDT_partial'
        self._open(trade_id, V12)
        partial = self._close(f'{trade_id}:partial', 'PARTIAL_TP')
        close = self._close(trade_id, 'TP')
        self.assertEqual(V12, partial['bot_version'])
        self.assertEqual(V12, close['bot_version'])
        self.assert_all_event_versions(trade_id, V12)

    def test_stale_reconciled_and_external_close_keep_opening_version(self):
        for suffix, reason in (
            ('stale', 'STALE_EXIT'),
            ('reconciled', 'RECONCILED_EXTERNAL_CLOSE'),
            ('external', 'EXTERNAL_CLOSE'),
        ):
            trade_id = f'short_AMDUSDT_{suffix}'
            self._open(trade_id, V12)
            close = self._close(trade_id, reason)
            self.assertEqual(V12, close['bot_version'])
            self.assert_all_event_versions(trade_id, V12)

    def test_v13_open_and_derived_events_remain_v13(self):
        trade_id = 'short_AMDUSDT_v13'
        self._open(trade_id, V13)
        self._close(f'{trade_id}:partial', 'PARTIAL_TP')
        self._close(trade_id, 'SL')
        self.assert_all_event_versions(trade_id, V13)

    def test_existing_event_version_is_not_overwritten(self):
        trade_id = 'short_AMDUSDT_explicit'
        self._open(trade_id, V12)
        close = self._close(trade_id, 'TP', bot_version=V12)
        self.assertEqual(V12, close['bot_version'])
        history_close = self._rows(self.trades_path)[-1]
        self.assertEqual(V12, history_close['bot_version'])

    def test_partial_uses_exact_base_and_never_crosses_same_symbol_trades(self):
        old_id = 'short_AMDUSDT_old'
        new_id = 'short_AMDUSDT_new'
        self._open(old_id, V12)
        self._open(new_id, V13)
        old_partial = self._close(f'{old_id}:partial', 'PARTIAL_TP')
        new_partial = self._close(f'{new_id}:partial', 'PARTIAL_TP')
        self.assertEqual(V12, old_partial['bot_version'])
        self.assertEqual(V13, new_partial['bot_version'])

    def test_missing_opening_is_explicitly_unresolved_without_runtime_fallback(self):
        with self.assertLogs(level='WARNING') as logs:
            close = self._close('short_UNKNOWNUSDT_missing', 'RECOVERED_CLOSE')
        self.assertNotIn('bot_version', close)
        self.assertEqual(
            'UNRESOLVED_DERIVED_EVENT_VERSION',
            close['bot_version_resolution']['classification'],
        )
        history_close = self._rows(self.trades_path)[-1]
        self.assertNotIn('bot_version', history_close)
        self.assertEqual(
            'UNRESOLVED_DERIVED_EVENT_VERSION',
            history_close['bot_version_resolution']['classification'],
        )
        self.assertTrue(any('version unresolved' in message for message in logs.output))

    def test_history_store_resolves_exact_open_if_caller_omits_version(self):
        trade_id = 'short_AMDUSDT_direct'
        self.store.record_trade_open(
            trade_id=trade_id,
            symbol='AMDUSDT',
            side='SHORT',
            entry_price=100,
            bot_version=V12,
        )
        close = self.store.record_trade_close(
            trade_id=trade_id,
            symbol='AMDUSDT',
            side='SHORT',
            entry_price=100,
            exit_price=90,
            exit_reason='TP',
            pnl_usdt=0.1,
        )
        self.assertEqual(V12, close['bot_version'])

    def test_near_and_zec_regression_fixtures_create_no_new_conflicts(self):
        fixtures = (
            'short_NEARUSDT_1785011704',
            'short_ZECUSDT_1785011585',
        )
        for trade_id in fixtures:
            self._open(trade_id, V12, symbol=trade_id.split('_')[1])
            self._close(trade_id, 'TP', symbol=trade_id.split('_')[1])
        report = validate(trades_path=self.trades_path, commit_checker=lambda _commit: True)
        conflicts = [
            issue for issue in report['errors']
            if issue['code'] == 'NEW_HISTORICAL_VERSION_CONFLICT'
        ]
        self.assertEqual([], conflicts)

    def test_resolution_contract_prefers_existing_event_context(self):
        resolution = resolve_derived_trade_version(
            'short_AMDUSDT_contract:partial',
            event_context={'bot_version': V12},
            trade_context={'bot_version': V13},
        )
        self.assertTrue(resolution['resolved'])
        self.assertEqual(V12, resolution['bot_version'])
        self.assertEqual('event_context', resolution['source'])


if __name__ == '__main__':
    unittest.main()
