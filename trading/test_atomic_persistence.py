#!/usr/bin/env python3
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(__file__))

import analytics
import atomic_persistence
import bot_state
import futures_reconciliation
import market
import operational_state
import post_cycle_check
import telegram_alerts
import telegram_commands
import utils


class AtomicPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp')
        self.root = Path(self.temp.name)

    def tearDown(self):
        for current, dirs, _files in os.walk(self.root, topdown=False):
            os.chmod(current, 0o700)
            for directory in dirs:
                path = os.path.join(current, directory)
                if not os.path.islink(path):
                    os.chmod(path, 0o700)
        self.temp.cleanup()

    def _symlink_fixture(self, relative=False):
        immutable = self.root / 'immutable'
        mutable = self.root / 'mutable'
        immutable.mkdir(exist_ok=True)
        mutable.mkdir(exist_ok=True)
        target = mutable / 'state.json'
        target.write_text('{"before":true}\n', encoding='utf-8')
        logical = immutable / 'state.json'
        link_target = os.path.relpath(target, immutable) if relative else str(target)
        logical.symlink_to(link_target)
        immutable.chmod(0o555)
        return immutable, mutable, logical, target, link_target

    def test_t1_normal_path_writes_complete_content(self):
        destination = self.root / 'normal.json'

        returned = atomic_persistence.atomic_write_text(destination, '{"ok":true}\n')

        self.assertEqual(str(destination), returned)
        self.assertEqual({'ok': True}, json.loads(destination.read_text(encoding='utf-8')))

    def test_t2_absolute_symlink_writes_in_mutable_target_directory(self):
        immutable, mutable, logical, target, link_target = self._symlink_fixture()

        atomic_persistence.atomic_write_text(logical, '{"after":true}\n')

        self.assertTrue(logical.is_symlink())
        self.assertEqual(link_target, os.readlink(logical))
        self.assertEqual({'after': True}, json.loads(target.read_text(encoding='utf-8')))
        self.assertFalse((immutable / 'state.json.tmp').exists())
        self.assertEqual([], list(mutable.glob('.state.json.*.tmp')))

    def test_t3_relative_symlink_is_preserved(self):
        _immutable, _mutable, logical, target, link_target = self._symlink_fixture(relative=True)

        atomic_persistence.atomic_write_text(logical, '{"relative":true}\n')

        self.assertTrue(logical.is_symlink())
        self.assertEqual(link_target, os.readlink(logical))
        self.assertEqual({'relative': True}, json.loads(target.read_text(encoding='utf-8')))

    def test_t4_preexisting_target_uses_atomic_replace(self):
        _immutable, _mutable, logical, target, _link_target = self._symlink_fixture()
        real_replace = os.replace
        with patch.object(atomic_persistence.os, 'replace', wraps=real_replace) as replace:
            atomic_persistence.atomic_write_text(logical, 'replacement\n')

        replace.assert_called_once()
        self.assertEqual(str(target), replace.call_args.args[1])
        self.assertEqual('replacement\n', target.read_text(encoding='utf-8'))

    def test_t5_missing_regular_destination_is_created(self):
        destination = self.root / 'new.json'

        atomic_persistence.atomic_write_text(destination, 'created\n')

        self.assertEqual('created\n', destination.read_text(encoding='utf-8'))

    def test_t6_replace_error_cleans_temp_and_preserves_target(self):
        _immutable, mutable, logical, target, _link_target = self._symlink_fixture()
        with patch.object(atomic_persistence.os, 'replace', side_effect=OSError('fixture replace failure')):
            with self.assertRaisesRegex(OSError, 'fixture replace failure'):
                atomic_persistence.atomic_write_text(logical, 'never committed\n')

        self.assertEqual('{"before":true}\n', target.read_text(encoding='utf-8'))
        self.assertTrue(logical.is_symlink())
        self.assertEqual([], list(mutable.glob('.state.json.*.tmp')))

    def test_t7_broken_symlink_fails_closed(self):
        logical = self.root / 'broken.json'
        missing = self.root / 'missing' / 'target.json'
        logical.symlink_to(missing)

        with self.assertRaises(atomic_persistence.AtomicPersistenceError):
            atomic_persistence.atomic_write_text(logical, 'blocked\n')

        self.assertFalse(missing.exists())
        self.assertTrue(logical.is_symlink())

    def test_t8_symlink_loop_fails_closed(self):
        first = self.root / 'first.json'
        second = self.root / 'second.json'
        first.symlink_to(second.name)
        second.symlink_to(first.name)

        with self.assertRaises(atomic_persistence.AtomicPersistenceError):
            atomic_persistence.atomic_write_text(first, 'blocked\n')

    def test_symlink_chain_resolves_to_regular_target(self):
        target = self.root / 'target.json'
        target.write_text('before\n', encoding='utf-8')
        middle = self.root / 'middle.json'
        logical = self.root / 'logical.json'
        middle.symlink_to(target.name)
        logical.symlink_to(middle.name)

        atomic_persistence.atomic_write_text(logical, 'after\n')

        self.assertTrue(logical.is_symlink())
        self.assertTrue(middle.is_symlink())
        self.assertEqual('after\n', target.read_text(encoding='utf-8'))

    def test_symlink_to_directory_fails_closed(self):
        directory = self.root / 'directory'
        directory.mkdir()
        logical = self.root / 'logical.json'
        logical.symlink_to(directory, target_is_directory=True)

        with self.assertRaises(atomic_persistence.AtomicPersistenceError):
            atomic_persistence.atomic_write_text(logical, 'blocked\n')

    def test_missing_parent_fails_closed(self):
        destination = self.root / 'missing' / 'state.json'

        with self.assertRaises(atomic_persistence.AtomicPersistenceError):
            atomic_persistence.atomic_write_text(destination, 'blocked\n')

    def test_t9_read_only_release_supports_all_migrated_mutable_paths(self):
        release = self.root / 'release'
        mutable = self.root / 'mutable'
        release_trading = release / 'trading'
        mutable_trading = mutable / 'trading'
        mutable_history = mutable / 'data' / 'history'
        reports = mutable_trading / 'reports'
        release_trading.mkdir(parents=True)
        mutable_trading.mkdir(parents=True)
        mutable_history.mkdir(parents=True)
        reports.mkdir()

        initial = {
            'state.json': '{}\n',
            'bot_state.json': '{}\n',
            'trade_analytics.jsonl': '',
            'decision_snapshots.jsonl': '',
            'trades_log.txt': '',
            'analysis_log.txt': '',
            '.cycle_baseline.json': '{}\n',
            'blacklist_dynamic.json': '{"symbols":[],"log":[]}\n',
            'telegram_alert_state.json': '{}\n',
            'telegram_offset.json': '{"offset":0}\n',
        }
        logical_paths = {}
        original_links = {}
        for name, content in initial.items():
            target = mutable_trading / name
            target.write_text(content, encoding='utf-8')
            logical = release_trading / name
            logical.symlink_to(target)
            logical_paths[name] = str(logical)
            original_links[name] = os.readlink(logical)
        (release / 'data').symlink_to(mutable / 'data', target_is_directory=True)
        (release_trading / 'reports').symlink_to(reports, target_is_directory=True)
        release_trading.chmod(0o555)
        release.chmod(0o555)

        metrics = {
            'timestamp': '2026-08-21T00:00:00Z',
            'trade_lines': 1,
            'decision_lines': 1,
            'trade_size_bytes': 1,
            'decision_size_bytes': 1,
            'state_open_positions': 0,
            'analytics_open_trades': 0,
            'trade_corrupt_lines': 0,
            'decision_corrupt_lines': 0,
        }
        stderr = io.StringIO()
        timeline = Mock()
        history_store = Mock()
        with contextlib.redirect_stderr(stderr), \
             patch.object(utils.config, 'STATE_FILE', logical_paths['state.json']), \
             patch.object(utils.config, 'TRADES_LOG', logical_paths['trades_log.txt']), \
             patch.object(utils.config, 'ANALYSIS_LOG', logical_paths['analysis_log.txt']), \
             patch.object(bot_state, 'BOT_STATE_FILE', logical_paths['bot_state.json']), \
             patch.object(post_cycle_check, 'BASELINE_FILE', logical_paths['.cycle_baseline.json']), \
             patch.object(market, '_DYNAMIC_BL_FILE', logical_paths['blacklist_dynamic.json']), \
             patch.object(market.utils, 'send_alert'), \
             patch.object(telegram_alerts, 'ALERT_STATE_FILE', logical_paths['telegram_alert_state.json']), \
             patch.object(telegram_commands, 'OFFSET_FILE', logical_paths['telegram_offset.json']):
            utils.save_state({'positions': []})
            bot_state.persist_bot_state({'fixture': 'bot-state'})
            analytics_logger = analytics.AnalyticsLogger(
                path=logical_paths['trade_analytics.jsonl'],
                history_store=history_store,
                timeline_recorder=timeline,
            )
            analytics_logger._append({'fixture': 'analytics'})
            snapshot_logger = analytics.DecisionSnapshotLogger(
                path=logical_paths['decision_snapshots.jsonl'],
                history_store=history_store,
                timeline_recorder=timeline,
            )
            snapshot_logger.log_snapshot(timestamp='2026-08-21T00:00:00Z')
            utils.log_trade(1, 'BTCUSDT', 'long', 'WIN', 1.0, 11.0)
            utils.log_analysis('long', None, {'MERCADO': 'offline fixture'})
            post_cycle_check._save_baseline(metrics)
            market._persist_blacklist('FIXTUREUSDT', 'offline fixture')
            telegram_alerts._write_state({'sent': 1})
            telegram_commands._save_offset(7)
            futures_reconciliation._write_json(
                str(release / 'data' / 'history' / 'futures_reconciliation_status.json'),
                {'status': 'ALINEADO'},
            )
            operational_state.append_event(
                {'event_type': 'fixture', 'observed_at': '2026-08-21T00:00:00Z'},
                path=str(release / 'data' / 'history' / 'operational_state.jsonl'),
            )
            analytics_logger.export_csv(str(release_trading / 'reports' / 'trades.csv'))

        self.assertNotIn('PermissionError', stderr.getvalue())
        self.assertNotIn('Permission denied', stderr.getvalue())
        for name, logical_path in logical_paths.items():
            self.assertTrue(os.path.islink(logical_path), name)
            self.assertEqual(original_links[name], os.readlink(logical_path), name)
        self.assertEqual('bot-state', json.loads((mutable_trading / 'bot_state.json').read_text(encoding='utf-8'))['fixture'])
        self.assertEqual(1, json.loads((mutable_trading / 'telegram_alert_state.json').read_text(encoding='utf-8'))['sent'])
        self.assertEqual(7, json.loads((mutable_trading / 'telegram_offset.json').read_text(encoding='utf-8'))['offset'])
        self.assertIn('FIXTUREUSDT', json.loads((mutable_trading / 'blacklist_dynamic.json').read_text(encoding='utf-8'))['symbols'])
        self.assertTrue((mutable_trading / 'trade_analytics.jsonl').stat().st_size)
        self.assertTrue((mutable_trading / 'decision_snapshots.jsonl').stat().st_size)
        self.assertTrue((mutable_trading / 'trades_log.txt').stat().st_size)
        self.assertTrue((mutable_trading / 'analysis_log.txt').stat().st_size)
        self.assertTrue((reports / 'trades.csv').is_file())
        self.assertTrue((mutable_history / 'futures_reconciliation_status.json').is_file())
        self.assertTrue((mutable_history / 'operational_state.jsonl').is_file())
        self.assertEqual([], list(mutable_trading.glob('.*.tmp')))
        self.assertEqual([], list(release_trading.glob('*.tmp')))

    def test_t10_repeated_writes_preserve_symlink_identity(self):
        _immutable, _mutable, logical, target, link_target = self._symlink_fixture(relative=True)

        for value in range(3):
            atomic_persistence.atomic_write_text(logical, json.dumps({'value': value}) + '\n')

        self.assertTrue(logical.is_symlink())
        self.assertEqual(link_target, os.readlink(logical))
        self.assertEqual({'value': 2}, json.loads(target.read_text(encoding='utf-8')))


class PostCutoverDetectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = Path(__file__).resolve().parent.parent / 'ops' / 'migrate_to_immutable_runtime.sh'
        source = script.read_text(encoding='utf-8')
        match = re.search(
            r"grep -Eqi '([^']+)'[^\n]*\n\s*die \"BLOCKED_POST_CUTOVER_LOG_ERROR\"",
            source,
        )
        if not match:
            raise AssertionError('post-cutover detector pattern not found')
        cls.pattern = re.compile(match.group(1), re.IGNORECASE)

    def test_permission_error_blocks(self):
        self.assertRegex('PermissionError: [Errno 13] denied', self.pattern)

    def test_permission_denied_blocks(self):
        self.assertRegex('OSError: Permission denied', self.pattern)

    def test_informational_permission_does_not_block(self):
        self.assertNotRegex('Informational permission policy loaded', self.pattern)


if __name__ == '__main__':
    unittest.main()
