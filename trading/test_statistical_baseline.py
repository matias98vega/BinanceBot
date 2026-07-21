import json
import os
import tempfile
import unittest
from unittest import mock

import statistical_baseline as sb


def row(i, win=None, version='v1', side='LONG', regime='bull', hour=None):
    hour = i if hour is None else hour
    return {
        'trade_id': f't{i}', 'classification': 'TRUSTED', 'is_closed': win is not None,
        'opening_timestamp': f'2026-01-{1 + hour // 24:02d}T{hour % 24:02d}:00:00Z',
        'closing_timestamp': f'2026-01-{1 + hour // 24:02d}T{hour % 24:02d}:30:00Z',
        'bot_version': version, 'side': side, 'regime': regime, 'symbol': f'S{i%4}',
        'features': {'atr': float(i + 1), 'side': side, 'regime': regime,
                     'symbol': f'S{i%4}', 'bot_version': version},
        'labels': {'binary_win': win, 'pnl_usdt': 1.0 if win else -1.0},
    }


class BaselineTests(unittest.TestCase):
    def test_trusted_closed_only(self):
        rows = [row(0, 1), row(1, 0), row(2, None)]
        rows[1]['classification'] = 'PARTIAL'
        with tempfile.NamedTemporaryFile('w', delete=False) as f:
            for item in rows:
                f.write(json.dumps(item) + '\n')
            path = f.name
        try:
            loaded, _, _ = sb.load_trusted(path)
            self.assertEqual(['t0'], [x['trade_id'] for x in loaded])
        finally:
            os.unlink(path)

    def test_dependency_same_hour(self):
        groups, report = sb.dependency_groups([row(0, 1, hour=0), row(1, 0, hour=0), row(2, 1, hour=2)])
        self.assertEqual([2, 1], list(map(len, groups)))
        self.assertEqual(2, report['group_count'])

    def test_groups_never_cross_split(self):
        groups, _ = sb.dependency_groups([row(i, i % 2, hour=i * 2) for i in range(120)])
        split = sb.temporal_split(groups, (20, 10, 10))
        ids = [{r['dependency_group_id'] for r in part} for part in split['parts']]
        self.assertFalse(ids[0] & ids[1] or ids[1] & ids[2] or ids[0] & ids[2])

    def test_split_reproducible(self):
        groups, _ = sb.dependency_groups([row(i, i % 2, hour=i * 2) for i in range(120)])
        self.assertEqual(sb.temporal_split(groups, (20, 10, 10))['profiles'],
                         sb.temporal_split(groups, (20, 10, 10))['profiles'])

    def test_split_insufficient(self):
        self.assertFalse(sb.temporal_split([[row(0, 1)]], (2, 2, 2))['viable'])

    def test_majority_uses_train(self):
        train = [row(i, 1 if i < 8 else 0) for i in range(10)]
        self.assertEqual([1, 1], sb._rule(train, [row(11, 0), row(12, 0)], 'majority').tolist())

    def test_prior(self):
        train = [row(i, i < 3) for i in range(4)]
        self.assertEqual(.75, sb._rule(train, [row(5, 0)], 'prior')[0])

    def test_side_fallback(self):
        train = [row(i, i % 2, side='LONG') for i in range(12)]
        self.assertEqual(.5, sb._rule(train, [row(20, 1, side='SHORT')], 'side')[0])

    def test_regime_fallback(self):
        train = [row(i, i % 2, regime='bull') for i in range(12)]
        self.assertEqual(.5, sb._rule(train, [row(20, 1, regime='bear')], 'regime')[0])

    def test_metrics_single_class(self):
        result = sb.metrics([1, 1], sb.np.array([.6, .8]))
        self.assertIsNone(result['roc_auc'])
        self.assertIn('brier_score', result)

    def test_economic_no_gains(self):
        result = sb.economic([row(0, 0), row(1, 0)], [.9, .9])
        self.assertEqual(0.0, result['profit_factor'])
        self.assertGreaterEqual(result['drawdown'], 0)

    def test_stability_detects_psi(self):
        a = [row(i, i % 2) for i in range(30)]
        b = [row(i + 40, i % 2) for i in range(15)]
        c = [row(i + 70, i % 2) for i in range(15)]
        for r in b + c:
            r['features']['atr'] += 1000
        self.assertEqual('HIGH_SHIFT', sb.stability(a, b, c, ['atr'], [])['features']['atr']['status'])

    def test_unseen_category(self):
        a = [row(i, i % 2, side='LONG') for i in range(20)]
        b = [row(30 + i, i % 2, side='SHORT') for i in range(10)]
        result = sb.stability(a, b, b, [], ['side'])
        self.assertEqual(['SHORT'], result['features']['side']['unseen_categories'])

    def test_fingerprint_stable(self):
        self.assertEqual(sb._fp({'x': 1}), sb._fp({'x': 1}))

    def test_no_output_by_default(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(sb, 'run_baseline', return_value={'fingerprint':'x','dataset_profile':{'trades':0},'dependency_groups':{'group_count':0},'temporal_split':{},'ready_for_xgboost_experiment':False,'blocking_reasons':['x']}):
                sb.main([])
            self.assertEqual([], os.listdir(d))

    def test_output_artifacts(self):
        with tempfile.TemporaryDirectory() as d:
            sample = {'fingerprint':'x'}
            sb.write_artifacts(sample, d)
            self.assertEqual(12, len(os.listdir(d)))


if __name__ == '__main__':
    unittest.main()
