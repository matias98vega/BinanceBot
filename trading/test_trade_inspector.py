#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))

import trade_inspector


class TradeInspectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.trades = os.path.join(self.tmp.name, 'trades.jsonl')
        self.decisions = os.path.join(self.tmp.name, 'decisions.jsonl')
        self.snapshots = os.path.join(self.tmp.name, 'snapshots.jsonl')
        self.timeline = os.path.join(self.tmp.name, 'timeline.jsonl')
        self.stats = os.path.join(self.tmp.name, 'stats.json')
        self._write_json(self.stats, {
            'by_symbol': {'ADAUSDT': {'closed': 1, 'pnl_total': 1.2}},
            'by_direction': {'SHORT': {'closed': 1, 'pnl_total': 1.2}},
        })

    def tearDown(self):
        self.tmp.cleanup()

    def _append(self, path, record):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record) + '\n')

    def _write_json(self, path, record):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(record, f)

    def _inspect(self, trade_id='t1'):
        return trade_inspector.inspect_trade(
            trade_id=trade_id,
            trades_file=self.trades,
            decisions_file=self.decisions,
            snapshots_file=self.snapshots,
            timeline_file=self.timeline,
            stats_file=self.stats,
        )

    def _normal_trade(self, trade_id='t1', symbol='ADAUSDT', side='SHORT', pnl=1.2, exit_reason='TP'):
        self._append(self.trades, {
            'event_type': 'TRADE_OPEN',
            'trade_id': trade_id,
            'symbol': symbol,
            'side': side,
            'opened_at': '2026-06-29T10:00:00Z',
            'entry_price': 1.0,
            'quantity': 20,
            'capital_used': 10,
            'score': 91,
            'btc_context': {'btc_price': 61000, 'trend': 'bearish'},
            'market_regime': 'bearish',
            'status': 'OPEN',
        })
        self._append(self.trades, {
            'event_type': 'TRADE_CLOSE',
            'trade_id': trade_id,
            'symbol': symbol,
            'side': side,
            'opened_at': '2026-06-29T10:00:00Z',
            'closed_at': '2026-06-29T12:00:00Z',
            'entry_price': 1.0,
            'exit_price': 0.94 if side == 'SHORT' else 1.06,
            'exit_reason': exit_reason,
            'pnl_usdt': pnl,
            'pnl_pct': 6.0,
            'status': 'CLOSED',
            'result': 'WIN' if pnl > 0 else 'LOSS',
        })
        self._append(self.decisions, {
            'timestamp': '2026-06-29T09:59:00Z',
            'decision': 'OPEN',
            'symbol': symbol,
            'side': side,
            'score': 91,
            'reason': 'ATR OK',
            'steps': ['BTC bearish', 'ATR OK', 'cooldown OK'],
            'market_regime': 'bearish',
        })
        self._append(self.snapshots, {
            'timestamp': '2026-06-29T09:59:30Z',
            'market_regime': 'bearish',
            'capital': {'total_real': 50, 'used': 10},
            'exposure': {'futures': 10},
            'candidates': [{'symbol': symbol, 'score': 91, 'reasons': ['trend', 'volume']}],
        })
        self._append(self.timeline, {
            'timestamp': '2026-06-29T10:00:01Z',
            'category': 'SIGNAL',
            'event': 'signal_evaluated',
            'symbol': symbol,
            'direction': side,
            'message': 'Signal accepted',
        })
        self._append(self.timeline, {
            'timestamp': '2026-06-29T10:00:02Z',
            'category': 'SIZING',
            'event': 'sizing_accepted',
            'symbol': symbol,
            'direction': side,
            'message': 'Futures allowed',
        })
        self._append(self.timeline, {
            'timestamp': '2026-06-29T10:00:03Z',
            'category': 'ORDER',
            'event': 'order_opened',
            'symbol': symbol,
            'direction': side,
            'message': 'SHORT opened',
            'related_trade_id': trade_id,
        })

    def test_trade_normal(self):
        self._normal_trade()

        report = self._inspect()

        self.assertTrue(report['found'])
        self.assertEqual(report['summary']['symbol'], 'ADAUSDT')
        self.assertEqual(report['market']['score'], 91)
        self.assertIn('Trade ganador siguiendo tendencia bajista', report['conclusion']['text'])

    def test_trade_with_recovery(self):
        self._normal_trade(symbol='ETHUSDT', side='LONG')
        self._append(self.timeline, {
            'timestamp': '2026-06-29T10:01:00Z',
            'category': 'PROTECTION',
            'event': 'recovery_success',
            'symbol': 'ETHUSDT',
            'direction': 'LONG',
            'message': 'OCO recovery successful',
            'related_trade_id': 't1',
        })

        report = self._inspect()

        self.assertEqual(report['protections']['recovery'], 'detectado')
        self.assertIn('Requirio recovery automatico', report['conclusion']['text'])

    def test_trade_with_guardian(self):
        self._normal_trade(pnl=-1.5, exit_reason='SL')
        self._append(self.timeline, {
            'timestamp': '2026-06-29T11:00:00Z',
            'category': 'GUARDIAN',
            'event': 'guardian_close',
            'symbol': 'ADAUSDT',
            'direction': 'SHORT',
            'message': 'Guardian close executed',
            'related_trade_id': 't1',
        })

        report = self._inspect()

        self.assertEqual(report['protections']['guardian'], 'detectado')
        self.assertIn('Guardian', report['conclusion']['text'])

    def test_trade_with_rebalance(self):
        self._normal_trade()
        self._append(self.timeline, {
            'timestamp': '2026-06-29T09:58:00Z',
            'category': 'REBALANCE',
            'event': 'rebalance_transfer',
            'symbol': 'ADAUSDT',
            'message': 'Transfer Spot -> Futures',
        })

        report = self._inspect()

        self.assertTrue(report['capital']['rebalance_applied'])
        self.assertEqual(report['capital']['available'], 50)

    def test_incomplete_data_and_corrupt_lines(self):
        self._append(self.trades, {
            'trade_id': 'broken',
            'symbol': 'XRPUSDT',
            'opened_at': '2026-06-29T10:00:00Z',
        })
        with open(self.timeline, 'a', encoding='utf-8') as f:
            f.write('{bad json}\n')

        report = self._inspect('broken')

        self.assertTrue(report['found'])
        self.assertEqual(report['summary']['direction'], trade_inspector.NOT_AVAILABLE)
        self.assertEqual(report['market']['regime'], trade_inspector.NOT_AVAILABLE)

    def test_latest_winner_and_loser(self):
        self._normal_trade('win', 'ADAUSDT', 'SHORT', 1.0, 'TP')
        self._normal_trade('loss', 'XRPUSDT', 'LONG', -1.0, 'SL')

        winner = trade_inspector.inspect_latest(
            result='WIN',
            trades_file=self.trades,
            decisions_file=self.decisions,
            snapshots_file=self.snapshots,
            timeline_file=self.timeline,
            stats_file=self.stats,
        )
        loser = trade_inspector.inspect_latest(
            result='LOSS',
            trades_file=self.trades,
            decisions_file=self.decisions,
            snapshots_file=self.snapshots,
            timeline_file=self.timeline,
            stats_file=self.stats,
        )

        self.assertEqual(winner['summary']['trade_id'], 'win')
        self.assertEqual(loser['summary']['trade_id'], 'loss')


if __name__ == '__main__':
    unittest.main()
