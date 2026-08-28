import contextlib
import io
import json
import os
import tempfile
import unittest

import capability_history
import check_version_consistency
import version_history


class CapabilityHistoryTests(unittest.TestCase):
    def test_registry_ids_and_commits_are_valid(self):
        ids = capability_history.capability_ids()
        self.assertEqual(len(ids), len(set(ids)))
        report = check_version_consistency.validate(trades_path='/missing', commit_checker=lambda commit: bool(commit))
        self.assertTrue(report['valid'], report)

    def test_behavioral_and_non_behavioral_contract(self):
        sizing = next(x for x in capability_history.CAPABILITIES if x['id'] == 'sizing-v2')
        capture = next(x for x in capability_history.CAPABILITIES if x['id'] == 'feature-capture-v2')
        self.assertTrue(sizing['behavioral'])
        self.assertEqual(('v1.2-sizing-v2',), sizing['bot_versions'])
        self.assertFalse(capture['behavioral'])

    def test_spot_market_guard_capability_references_behavior_commit(self):
        capability = next(x for x in capability_history.CAPABILITIES if x['id'] == 'spot-market-status-guard-v1')
        self.assertEqual('ACTIVE', capability['status'])
        self.assertTrue(capability['behavioral'])
        self.assertTrue(capability['affects_execution'])
        self.assertFalse(capability['affects_trade_selection'])
        self.assertFalse(capability['affects_trade_management'])
        self.assertFalse(capability['affects_accounting'])
        self.assertTrue(capability['affects_observability'])
        self.assertEqual(('v1.4-spot-market-status-guard',), capability['bot_versions'])
        self.assertEqual('partial-quantity-integrity-v1', capability['predecessor'])
        self.assertEqual('7f3c8dcffdeb699a913b6324159b88a081b04d22', capability['introduced_by_commit'])
        self.assertTrue(capability_history.requires_bot_version({'affects_execution': True}))

    def test_preventive_futures_close_capability_references_fix_commit(self):
        capability = next(
            item for item in capability_history.CAPABILITIES
            if item['id'] == 'preventive-futures-close-confirmation-v1'
        )
        self.assertEqual('IMPLEMENTED', capability['status'])
        self.assertTrue(capability['behavioral'])
        self.assertTrue(capability['affects_execution'])
        self.assertTrue(capability['affects_trade_management'])
        self.assertTrue(capability['affects_observability'])
        self.assertFalse(capability['affects_trade_selection'])
        self.assertFalse(capability['affects_accounting'])
        self.assertEqual(('v1.5-preventive-futures-close-fix',), capability['bot_versions'])
        self.assertEqual('spot-market-status-guard-v1', capability['predecessor'])
        self.assertEqual('7b181a17be705febb6b0a4d941a51ff356b609c0', capability['introduced_by_commit'])
        self.assertIn('LONG Spot is not covered', capability['notes'])

    def test_gate_and_model_modes(self):
        self.assertFalse(capability_history.requires_bot_version({'pre_entry_gate_mode': 'AUDIT_ONLY'}))
        self.assertTrue(capability_history.requires_bot_version({'pre_entry_gate_mode': 'ENFORCE'}))
        self.assertFalse(capability_history.requires_bot_version({'model_mode': 'SHADOW_READ_ONLY'}))
        self.assertTrue(capability_history.requires_bot_version({'model_mode': 'LIVE_FILTER'}))

    def test_feature_schema_is_independent_and_no_model_is_deployed(self):
        report = check_version_consistency.validate(trades_path='/missing', commit_checker=lambda commit: True)
        self.assertTrue(report['feature_schema_independent'])
        self.assertIsNone(report['deployed_model_version'])
        self.assertEqual('v1.6-preventive-spot-close-fix', version_history.current_version())

    def _write_release_metadata(self, directory, *, tamper=False):
        release_commit = '9cb86796645b913c844e1170729a745f482fbb98'
        commits = sorted({str(item.get('introduced_by_commit') or '') for item in capability_history.CAPABILITIES})
        if tamper:
            commits = commits[:-1]
        metadata = {
            'schema_version': 1,
            'release_commit': release_commit,
            'registry_sha256': check_version_consistency._registry_sha256(),
            'verified_commits': commits,
        }
        with open(os.path.join(directory, '.release-commit'), 'w', encoding='ascii') as stream:
            stream.write(release_commit + '\n')
        with open(os.path.join(directory, '.release-version-commits.json'), 'w', encoding='utf-8') as stream:
            json.dump(metadata, stream)

    def test_gitless_release_uses_registry_bound_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_release_metadata(tmp)
            report = check_version_consistency.validate(project_dir=tmp, trades_path='/missing')
        self.assertTrue(report['strict_valid'], report)
        self.assertEqual('release_metadata', report['commit_validation_source'])
        self.assertEqual('9cb86796645b913c844e1170729a745f482fbb98', report['release_commit'])

    def test_gitless_tree_without_release_metadata_remains_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = check_version_consistency.validate(project_dir=tmp, trades_path='/missing')
        self.assertFalse(report['strict_valid'])
        self.assertEqual('unavailable', report['commit_validation_source'])
        self.assertTrue(all(item['code'] == 'UNKNOWN_INTRODUCING_COMMIT' for item in report['errors']))

    def test_tampered_release_metadata_remains_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_release_metadata(tmp, tamper=True)
            report = check_version_consistency.validate(project_dir=tmp, trades_path='/missing')
        self.assertFalse(report['strict_valid'])
        self.assertEqual('unavailable', report['commit_validation_source'])


class TradeVersionConsistencyTests(unittest.TestCase):
    def _report(self, rows):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trades.jsonl')
            with open(path, 'w', encoding='utf-8') as stream:
                for row in rows:
                    stream.write(json.dumps(row) + '\n')
            with open(path, 'rb') as stream:
                before = stream.read()
            report = check_version_consistency.validate(trades_path=path, commit_checker=lambda commit: True)
            with open(path, 'rb') as stream:
                after = stream.read()
            self.assertEqual(before, after, 'validator must be read-only')
            return report

    def test_opening_version_survives_close_and_partial(self):
        rows = [
            {'event_type':'TRADE_OPEN','status':'OPEN','trade_id':'t1','bot_version':'v1.0-alpha'},
            {'event_type':'TRADE_CLOSE','status':'CLOSED','trade_id':'t1:partial','bot_version':'v1.0-alpha'},
            {'event_type':'TRADE_CLOSE','status':'CLOSED','trade_id':'t1','bot_version':'v1.0-alpha'},
        ]
        self.assertTrue(self._report(rows)['valid'])

    def test_legacy_unknown_is_explicit_warning(self):
        report = self._report([{'event_type':'TRADE_CLOSE','trade_id':'legacy-close'}])
        self.assertTrue(report['valid'])
        self.assertEqual('LEGACY_OR_UNMATCHED_CLOSE', report['warnings'][0]['code'])

    def test_known_conflict_is_warning_and_strict_valid(self):
        trade_id = 'short_EWYUSDT_1783476970'
        report = self._report([
            {'event_type':'TRADE_OPEN','status':'OPEN','trade_id':trade_id,'bot_version':'v1.0-alpha'},
            {'event_type':'TRADE_CLOSE','trade_id':trade_id,'bot_version':'v1.1-observability-hardening'},
        ])
        self.assertTrue(report['strict_valid'])
        self.assertEqual('KNOWN_IMMUTABLE_HISTORICAL_VERSION_CONFLICT', report['warnings'][0]['code'])
        self.assertEqual('v1.0-alpha', report['warnings'][0]['canonical_version'])

    def test_new_conflict_fails_strict(self):
        report = self._report([
            {'event_type':'TRADE_OPEN','status':'OPEN','trade_id':'new','bot_version':'v1.0-alpha'},
            {'event_type':'TRADE_CLOSE','trade_id':'new','bot_version':'v1.1-observability-hardening'},
        ])
        self.assertFalse(report['strict_valid'])
        self.assertEqual('NEW_HISTORICAL_VERSION_CONFLICT', report['errors'][0]['code'])

    def test_cli_json_explain_and_strict(self):
        for args in (['--json'], ['--explain'], ['--strict']):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = check_version_consistency.main(args + ['--trades', '/missing'])
            self.assertEqual(0, code)
            self.assertTrue(output.getvalue().strip())


if __name__ == '__main__':
    unittest.main()
