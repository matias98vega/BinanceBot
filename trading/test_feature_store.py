#!/usr/bin/env python3
import json
import math
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import feature_store


class FeatureStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'features.jsonl')

    def tearDown(self):
        self.tmp.cleanup()

    def _base(self):
        return {
            'trade_id': 't1',
            'timestamp': '2026-06-30T12:00:00Z',
            'symbol': 'ADAUSDT',
            'side': 'SHORT',
            'wallet': 'futures',
            'bot_version': 'test',
            'market_regime': 'bearish',
            'btc_context': {'btc_price': 61000, 'btc_change_4h': -1.2, 'api_key': 'secret'},
            'entry_price': 1.0,
            'ema20': 1.05,
            'ema50': 1.1,
            'ema200': 1.2,
            'rsi': 71,
            'macd': -0.1,
            'macd_hist': -0.02,
            'atr': 0.03,
            'volume_ratio': 1.8,
            'score': 91,
            'score_min_required': 80,
            'reasons': ['BTC bearish', 'ATR OK'],
            'capital_spot': 10,
            'capital_futures': 40,
            'capital_total': 50,
            'exposure_pct': 20,
            'position_calculated': 19.3,
            'position_final': 19.0,
            'quantity': 31.5,
            'leverage': 3,
            'open_longs': 0,
            'open_shorts': 1,
            'active_cooldowns': 2,
            'guardian_active': True,
            'directional_mode': 'bearish',
            'open_reason': 'score',
            'snapshot_id': 's1',
            'decision_id': 'd1',
            'timeline_id': 'tl1',
            'extra': {'token': 'secret', 'safe': 'ok'},
        }

    def test_record_creates_jsonl_with_required_fields(self):
        record = feature_store.record_trade_features(features_file=self.path, **self._base())

        self.assertTrue(os.path.exists(self.path))
        with open(self.path, encoding='utf-8') as f:
            row = json.loads(f.readline())
        self.assertEqual(record['identification']['trade_id'], 't1')
        self.assertEqual(row['identification']['symbol'], 'ADAUSDT')
        self.assertEqual(row['identification']['direction'], 'SHORT')
        self.assertEqual(row['market']['regime'], 'bear')
        self.assertEqual(row['market']['btc_regime'], 'bearish')
        self.assertEqual(row['market']['hour_utc'], 12)
        self.assertEqual(row['market']['weekday'], 1)

    def test_append_correctly(self):
        feature_store.record_trade_features(features_file=self.path, **self._base())
        second = self._base()
        second['trade_id'] = 't2'
        feature_store.record_trade_features(features_file=self.path, **second)

        with open(self.path, encoding='utf-8') as f:
            rows = [json.loads(line) for line in f if line.strip()]
        self.assertEqual([r['identification']['trade_id'] for r in rows], ['t1', 't2'])

    def test_optional_fields_are_null_when_missing(self):
        record = feature_store.record_trade_features(
            features_file=self.path,
            trade_id='minimal',
            symbol='BTCUSDT',
        )

        self.assertIsNone(record['identification']['direction'])
        self.assertEqual(record['market']['regime'], 'unknown')
        self.assertIsNone(record['market']['btc_price'])
        self.assertIsNone(record['symbol_indicators']['ema200'])
        self.assertEqual(record['scoring']['reason_count'], 0)

    def test_sanitizes_sensitive_and_nan_values(self):
        data = self._base()
        data['score'] = math.nan
        data['extra'] = {'api_secret': 'hidden', 'nested': {'authorization': 'bearer', 'value': 3}}

        record = feature_store.record_trade_features(features_file=self.path, **data)

        self.assertIsNone(record['scoring']['score_total'])
        self.assertNotIn('api_secret', record['extra'])
        self.assertNotIn('authorization', record['extra']['nested'])
        self.assertEqual(record['extra']['nested']['value'], 3)

    def test_corrupt_existing_file_does_not_block_append(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write('{bad json}\n')

        feature_store.record_trade_features(features_file=self.path, **self._base())

        with open(self.path, encoding='utf-8') as f:
            lines = [line.strip() for line in f if line.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[-1])['identification']['trade_id'], 't1')

    def test_write_error_is_tolerated(self):
        with patch('builtins.open', side_effect=OSError('disk full')):
            with self.assertLogs(level='WARNING') as logs:
                result = feature_store.record_trade_features(features_file=self.path, **self._base())

        self.assertIsNone(result)
        self.assertIn('feature_store write failed', '\n'.join(logs.output))

    def test_analytics_integration_does_not_change_flow(self):
        import analytics

        logger = analytics.AnalyticsLogger(path=os.path.join(self.tmp.name, 'analytics.jsonl'))
        with patch('feature_store.record_trade_features', side_effect=OSError('no write')):
            record = logger.log_trade_open(
                trade_id='t1',
                symbol='ADAUSDT',
                side='SHORT',
                entry_price=1.0,
                score=90,
                entry_time='2026-06-30T12:00:00Z',
            )

        self.assertEqual(record['trade_id'], 't1')
        self.assertEqual(record['status'], 'OPEN')


if __name__ == '__main__':
    unittest.main()
