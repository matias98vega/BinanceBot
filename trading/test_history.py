#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))

import history


class HistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = self.tmp.name
        self.store = history.HistoryStore(
            trades_file=os.path.join(base, 'trades.jsonl'),
            decisions_file=os.path.join(base, 'decisions.jsonl'),
            snapshots_file=os.path.join(base, 'snapshots.jsonl'),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _read_lines(self, path):
        with open(path, encoding='utf-8') as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_record_trade_open_creates_file_and_open_trade(self):
        record = self.store.record_trade_open(
            trade_id='t1',
            symbol='ETHUSDT',
            side='LONG',
            opened_at=1710000000,
            entry_price=100,
            quantity=0.5,
            capital_used=50,
            wallet='SPOT',
            score=7,
            atr=1.2,
            atr_pct=1.2,
            rsi=55,
            market_regime='bullish',
        )

        self.assertTrue(os.path.exists(self.store.trades_file))
        self.assertEqual(record['status'], 'OPEN')
        self.assertEqual(record['trade_id'], 't1')
        self.assertEqual(record['wallet'], 'SPOT')
        self.assertEqual(record['capital_used'], 50.0)
        self.assertEqual(record['regime'], 'bull')

    def test_record_trade_close_appends_and_marks_closed(self):
        self.store.record_trade_open(
            trade_id='t1',
            symbol='ETHUSDT',
            side='LONG',
            opened_at='2026-01-01T00:00:00Z',
            entry_price=100,
        )
        close = self.store.record_trade_close(
            trade_id='t1',
            symbol='ETHUSDT',
            side='LONG',
            opened_at='2026-01-01T00:00:00Z',
            closed_at='2026-01-01T01:00:00Z',
            entry_price=100,
            exit_price=110,
            exit_reason='TP',
            pnl_usdt=5,
        )

        lines = self._read_lines(self.store.trades_file)
        self.assertEqual(len(lines), 2)
        self.assertEqual(close['status'], 'CLOSED')
        self.assertEqual(close['result'], 'WIN')
        self.assertEqual(close['duration_minutes'], 60.0)
        self.assertEqual(close['pnl_pct'], 10.0)

    def test_get_trade_missing_file_returns_none(self):
        self.assertIsNone(self.store.get_trade('missing'))

    def test_get_trade_skips_invalid_json(self):
        os.makedirs(os.path.dirname(self.store.trades_file), exist_ok=True)
        with open(self.store.trades_file, 'w', encoding='utf-8') as f:
            f.write('{invalid json}\n')
            f.write(json.dumps({'trade_id': 't1', 'status': 'OPEN'}) + '\n')

        with self.assertLogs(level='WARNING') as logs:
            trade = self.store.get_trade('t1')

        self.assertEqual(trade['status'], 'OPEN')
        self.assertTrue(any('history JSONL invalid' in line for line in logs.output))

    def test_get_trade_merges_open_and_closed_records(self):
        self.store.record_trade_open(
            trade_id='t1',
            symbol='BTCUSDT',
            side='SHORT',
            opened_at='2026-01-01T00:00:00Z',
            entry_price=100,
            wallet='FUTURES',
        )
        self.store.record_trade_close(
            trade_id='t1',
            symbol='BTCUSDT',
            side='SHORT',
            opened_at='2026-01-01T00:00:00Z',
            closed_at='2026-01-01T00:30:00Z',
            entry_price=100,
            exit_price=90,
            exit_reason='SL',
            pnl_usdt=-1,
        )

        trade = self.store.get_trade('t1')
        self.assertEqual(trade['status'], 'CLOSED')
        self.assertEqual(trade['symbol'], 'BTCUSDT')
        self.assertEqual(trade['wallet'], 'FUTURES')
        self.assertEqual(trade['result'], 'LOSS')

    def test_record_decision_and_snapshot_append(self):
        self.store.record_decision(
            decision='OPEN',
            symbol='ETHUSDT',
            side='SHORT',
            reason='score_ok',
            steps=['BTC bearish', 'ATR OK', 'cooldown OK'],
            score=91,
        )
        self.store.record_snapshot(
            market={'btc_trend': 'bearish'},
            capital={'spot': 10, 'futures': 40},
            exposure={'short': 20},
            max_positions={'short': 2},
        )

        decisions = self._read_lines(self.store.decisions_file)
        snapshots = self._read_lines(self.store.snapshots_file)
        self.assertEqual(decisions[0]['event_type'], 'DECISION')
        self.assertEqual(decisions[0]['steps'][0], 'BTC bearish')
        self.assertEqual(decisions[0]['regime'], 'unknown')
        self.assertEqual(snapshots[0]['event_type'], 'MARKET_SNAPSHOT')
        self.assertEqual(snapshots[0]['regime'], 'bear')
        self.assertEqual(snapshots[0]['capital']['futures'], 40)


if __name__ == '__main__':
    unittest.main()
