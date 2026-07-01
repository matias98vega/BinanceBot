#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))

import analytics_engine
import history


class AnalyticsEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = self.tmp.name
        self.trades = os.path.join(base, 'trades.jsonl')
        self.decisions = os.path.join(base, 'decisions.jsonl')
        self.snapshots = os.path.join(base, 'snapshots.jsonl')
        self.stats = os.path.join(base, 'stats.json')
        self.store = history.HistoryStore(self.trades, self.decisions, self.snapshots)

    def tearDown(self):
        self.tmp.cleanup()

    def _rebuild(self, stats_file=None):
        return analytics_engine.rebuild_statistics(
            trades_file=self.trades,
            decisions_file=self.decisions,
            snapshots_file=self.snapshots,
            stats_file=stats_file or self.stats,
        )

    def _load(self):
        return analytics_engine.load_stats(
            stats_file=self.stats,
            trades_file=self.trades,
            decisions_file=self.decisions,
            snapshots_file=self.snapshots,
        )

    def _seed_closed_trades(self):
        self.store.record_trade_open(
            trade_id='t1',
            symbol='ETHUSDT',
            side='LONG',
            opened_at='2026-01-01T00:00:00Z',
            entry_price=100,
            market_regime='bullish',
            wallet='SPOT',
        )
        self.store.record_trade_close(
            trade_id='t1',
            symbol='ETHUSDT',
            side='LONG',
            opened_at='2026-01-01T00:00:00Z',
            closed_at='2026-01-01T01:00:00Z',
            entry_price=100,
            exit_price=110,
            exit_reason='TP',
            pnl_usdt=10,
        )
        self.store.record_trade_open(
            trade_id='t2',
            symbol='BTCUSDT',
            side='SHORT',
            opened_at='2026-01-02T02:00:00Z',
            entry_price=100,
            market_regime='bearish',
            wallet='FUTURES',
        )
        self.store.record_trade_close(
            trade_id='t2',
            symbol='BTCUSDT',
            side='SHORT',
            opened_at='2026-01-02T02:00:00Z',
            closed_at='2026-01-02T03:00:00Z',
            entry_price=100,
            exit_price=105,
            exit_reason='SL',
            pnl_usdt=-5,
        )
        self.store.record_trade_open(
            trade_id='t3',
            symbol='ETHUSDT',
            side='LONG',
            opened_at='2026-01-03T04:00:00Z',
            entry_price=100,
            market_regime='neutral',
        )
        self.store.record_decision(decision='OPEN', symbol='ETHUSDT', side='LONG', reason='score_ok')
        self.store.record_snapshot(market={'btc_trend': 'bullish'}, capital={'spot': 50})

    def test_rebuild_statistics_full(self):
        self._seed_closed_trades()

        stats = self._rebuild()

        self.assertEqual(stats['general']['total_trades'], 3)
        self.assertEqual(stats['general']['open_trades'], 1)
        self.assertEqual(stats['general']['closed_trades'], 2)
        self.assertEqual(stats['general']['win'], 1)
        self.assertEqual(stats['general']['loss'], 1)
        self.assertEqual(stats['general']['pnl_total'], 5.0)
        self.assertEqual(stats['general']['pnl_daily']['2026-01-01'], 10.0)
        self.assertEqual(stats['general']['pnl_daily']['2026-01-02'], -5.0)
        self.assertEqual(stats['decisions']['total'], 1)
        self.assertEqual(stats['snapshots']['total'], 1)

    def test_win_rate_profit_factor_and_expectancy(self):
        self._seed_closed_trades()

        general = self._rebuild()['general']

        self.assertEqual(general['win_rate'], 50.0)
        self.assertEqual(general['profit_factor'], 2.0)
        self.assertEqual(general['expectancy'], 2.5)

    def test_symbol_direction_regime_and_exit_reason_stats(self):
        self._seed_closed_trades()

        stats = self._rebuild()

        self.assertEqual(stats['by_symbol']['ETHUSDT']['trades'], 2)
        self.assertEqual(stats['by_symbol']['ETHUSDT']['pnl_total'], 10.0)
        self.assertEqual(stats['by_direction']['LONG']['win_rate'], 100.0)
        self.assertEqual(stats['by_direction']['SHORT']['win_rate'], 0.0)
        self.assertEqual(stats['by_regime']['bull']['pnl_total'], 10.0)
        self.assertEqual(stats['by_regime']['bear']['pnl_total'], -5.0)
        self.assertEqual(stats['by_exit_reason']['TP']['closed'], 1)
        self.assertEqual(stats['by_exit_reason']['SL']['closed'], 1)

    def test_regime_direct_field(self):
        self.store._append(self.trades, {
            'trade_id': 'direct',
            'symbol': 'ETHUSDT',
            'side': 'LONG',
            'opened_at': '2026-01-01T00:00:00Z',
            'closed_at': '2026-01-01T01:00:00Z',
            'status': 'CLOSED',
            'regime': 'bear',
            'pnl_usdt': 1,
        })

        stats = self._rebuild()

        self.assertEqual(stats['by_regime']['bear']['closed'], 1)
        self.assertEqual(stats['by_regime']['unknown']['closed'], 0)

    def test_regime_market_regime_legacy(self):
        self.store._append(self.trades, {
            'trade_id': 'legacy_market',
            'symbol': 'ETHUSDT',
            'side': 'LONG',
            'status': 'CLOSED',
            'market_regime': 'bullish',
            'pnl_usdt': 1,
        })

        stats = self._rebuild()

        self.assertEqual(stats['by_regime']['bull']['closed'], 1)

    def test_regime_btc_regime_legacy(self):
        self.store._append(self.trades, {
            'trade_id': 'legacy_btc',
            'symbol': 'ETHUSDT',
            'side': 'LONG',
            'status': 'CLOSED',
            'btc_regime': 'BEAR',
            'pnl_usdt': 1,
        })

        stats = self._rebuild()

        self.assertEqual(stats['by_regime']['bear']['closed'], 1)

    def test_regime_uppercase_bearish_normalizes(self):
        self.store._append(self.trades, {
            'trade_id': 'upper',
            'symbol': 'ETHUSDT',
            'side': 'LONG',
            'status': 'CLOSED',
            'market_regime': 'BEARISH',
            'pnl_usdt': 1,
        })

        stats = self._rebuild()

        self.assertEqual(stats['by_regime']['bear']['closed'], 1)

    def test_missing_regime_is_unknown(self):
        self.store._append(self.trades, {
            'trade_id': 'missing',
            'symbol': 'ETHUSDT',
            'side': 'LONG',
            'status': 'CLOSED',
            'pnl_usdt': 1,
        })

        stats = self._rebuild()

        self.assertEqual(stats['by_regime']['unknown']['closed'], 1)

    def test_legacy_regimes_do_not_all_go_unknown(self):
        self.store._append(self.trades, {
            'trade_id': 'm1',
            'symbol': 'ETHUSDT',
            'side': 'LONG',
            'status': 'CLOSED',
            'market_regime': 'bullish',
            'pnl_usdt': 1,
        })
        self.store._append(self.trades, {
            'trade_id': 'm2',
            'symbol': 'BTCUSDT',
            'side': 'SHORT',
            'status': 'CLOSED',
            'btc_regime': 'bearish',
            'pnl_usdt': 1,
        })

        stats = self._rebuild()

        self.assertEqual(stats['by_regime']['bull']['closed'], 1)
        self.assertEqual(stats['by_regime']['bear']['closed'], 1)
        self.assertEqual(stats['by_regime']['unknown']['closed'], 0)

    def test_load_stats_missing_rebuilds(self):
        self._seed_closed_trades()

        stats = self._load()

        self.assertTrue(os.path.exists(self.stats))
        self.assertEqual(stats['general']['closed_trades'], 2)

    def test_load_stats_corrupt_rebuilds_with_warning(self):
        self._seed_closed_trades()
        os.makedirs(os.path.dirname(self.stats), exist_ok=True)
        with open(self.stats, 'w', encoding='utf-8') as f:
            f.write('{invalid json')

        with self.assertLogs(level='WARNING') as logs:
            stats = self._load()

        self.assertEqual(stats['general']['closed_trades'], 2)
        self.assertTrue(any('stats.json corrupt' in line for line in logs.output))

    def test_incremental_update_matches_rebuild(self):
        self.store.record_trade_open(
            trade_id='t1',
            symbol='ETHUSDT',
            side='LONG',
            opened_at='2026-01-01T00:00:00Z',
            entry_price=100,
            market_regime='bullish',
        )
        analytics_engine.rebuild_statistics(self.trades, self.decisions, self.snapshots, self.stats)
        close = self.store.record_trade_close(
            trade_id='t1',
            symbol='ETHUSDT',
            side='LONG',
            opened_at='2026-01-01T00:00:00Z',
            closed_at='2026-01-01T01:00:00Z',
            entry_price=100,
            exit_price=110,
            exit_reason='TP',
            pnl_usdt=10,
        )

        incremental = analytics_engine.update_trade(
            close,
            stats_file=self.stats,
            trades_file=self.trades,
            decisions_file=self.decisions,
            snapshots_file=self.snapshots,
        )
        rebuild_stats_file = os.path.join(self.tmp.name, 'stats_rebuild.json')
        rebuilt = self._rebuild(rebuild_stats_file)

        self.assertEqual(incremental, rebuilt)

    def test_getters_read_stats_json_only(self):
        self._seed_closed_trades()
        self._rebuild()

        os.remove(self.trades)
        general = analytics_engine.get_general_stats(self.stats)
        symbol = analytics_engine.get_symbol_stats('ETHUSDT', self.stats)
        direction = analytics_engine.get_direction_stats('LONG', self.stats)
        reason = analytics_engine.get_exit_reason_stats('TP', self.stats)
        time_stats = analytics_engine.get_time_stats('day', self.stats)

        self.assertEqual(general['closed_trades'], 2)
        self.assertEqual(symbol['pnl_total'], 10.0)
        self.assertEqual(direction['win_rate'], 100.0)
        self.assertEqual(reason['closed'], 1)
        self.assertIn('2026-01-01', time_stats)


if __name__ == '__main__':
    unittest.main()
