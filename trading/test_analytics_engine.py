#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))

import analytics_engine
import capital_ledger
import history


class AnalyticsEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = self.tmp.name
        self.trades = os.path.join(base, 'trades.jsonl')
        self.decisions = os.path.join(base, 'decisions.jsonl')
        self.snapshots = os.path.join(base, 'snapshots.jsonl')
        self.features = os.path.join(base, 'features.jsonl')
        self.stats = os.path.join(base, 'stats.json')
        self.ledger = os.path.join(base, 'capital_ledger.jsonl')
        self.store = history.HistoryStore(self.trades, self.decisions, self.snapshots)

    def tearDown(self):
        self.tmp.cleanup()

    def _rebuild(self, stats_file=None):
        return analytics_engine.rebuild_statistics(
            trades_file=self.trades,
            decisions_file=self.decisions,
            snapshots_file=self.snapshots,
            stats_file=stats_file or self.stats,
            features_file=self.features,
        )

    def _load(self):
        return analytics_engine.load_stats(
            stats_file=self.stats,
            trades_file=self.trades,
            decisions_file=self.decisions,
            snapshots_file=self.snapshots,
            features_file=self.features,
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

    def test_rebuild_uses_feature_store_regime_when_trade_close_lacks_regime(self):
        self.store._append(self.trades, {
            'trade_id': 'short_ADAUSDT_1',
            'symbol': 'ADAUSDT',
            'side': 'SHORT',
            'status': 'CLOSED',
            'closed_at': '2026-07-09T12:00:00Z',
            'pnl_usdt': 1.2,
        })
        self.store._append(self.features, {
            'identification': {'trade_id': 'short_ADAUSDT_1', 'timestamp': '2026-07-09T11:00:00Z'},
            'market': {'regime': 'bullish'},
        })

        stats = self._rebuild()

        self.assertEqual(stats['by_regime']['bull']['closed'], 1)
        self.assertEqual(stats['by_regime']['unknown']['closed'], 0)
        self.assertEqual(stats['trade_index']['short_ADAUSDT_1']['regime'], 'bull')

    def test_rebuild_uses_base_trade_id_for_partial_feature_regime(self):
        self.store._append(self.trades, {
            'trade_id': 'short_ADAUSDT_2:partial',
            'symbol': 'ADAUSDT',
            'side': 'SHORT',
            'status': 'CLOSED',
            'exit_reason': 'PARTIAL',
            'pnl_usdt': 0.4,
        })
        self.store._append(self.features, {
            'identification': {'trade_id': 'short_ADAUSDT_2'},
            'market': {'regime': 'bear'},
        })

        stats = self._rebuild()

        self.assertEqual(stats['by_regime']['bear']['closed'], 1)
        self.assertEqual(stats['by_regime']['unknown']['closed'], 0)

    def test_update_trade_uses_feature_store_regime_when_index_lacks_open(self):
        self.store._append(self.features, {
            'identification': {'trade_id': 'short_NEARUSDT_1', 'timestamp': '2026-07-09T10:00:00Z'},
            'market': {'regime': 'sideways'},
        })
        analytics_engine.rebuild_statistics(
            trades_file=self.trades,
            decisions_file=self.decisions,
            snapshots_file=self.snapshots,
            stats_file=self.stats,
            features_file=self.features,
        )

        stats = analytics_engine.update_trade(
            {
                'trade_id': 'short_NEARUSDT_1',
                'symbol': 'NEARUSDT',
                'side': 'SHORT',
                'status': 'CLOSED',
                'closed_at': '2026-07-09T11:00:00Z',
                'pnl_usdt': 0.7,
            },
            stats_file=self.stats,
            trades_file=self.trades,
            decisions_file=self.decisions,
            snapshots_file=self.snapshots,
            features_file=self.features,
        )

        self.assertEqual(stats['by_regime']['sideways']['closed'], 1)
        self.assertEqual(stats['by_regime']['unknown']['closed'], 0)

    def test_load_stats_old_schema_rebuilds_with_feature_regime(self):
        self.store._append(self.trades, {
            'trade_id': 'long_SOLUSDT_1',
            'symbol': 'SOLUSDT',
            'side': 'LONG',
            'status': 'CLOSED',
            'pnl_usdt': 2,
        })
        self.store._append(self.features, {
            'identification': {'trade_id': 'long_SOLUSDT_1'},
            'market': {'regime': 'neutral'},
        })
        with open(self.stats, 'w', encoding='utf-8') as f:
            json.dump({'schema_version': 1, 'by_regime': {'unknown': {'closed': 1}}}, f)

        stats = self._load()

        self.assertEqual(stats['schema_version'], analytics_engine.STATS_SCHEMA_VERSION)
        self.assertEqual(stats['by_regime']['neutral']['closed'], 1)
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
        analytics_engine.rebuild_statistics(
            self.trades,
            self.decisions,
            self.snapshots,
            self.stats,
            features_file=self.features,
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
            pnl_usdt=10,
        )

        incremental = analytics_engine.update_trade(
            close,
            stats_file=self.stats,
            trades_file=self.trades,
            decisions_file=self.decisions,
            snapshots_file=self.snapshots,
            features_file=self.features,
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

    def test_capital_accounting_ledger_missing_and_empty(self):
        missing_ledger = os.path.join(self.tmp.name, 'missing_ledger.jsonl')

        self.assertEqual(analytics_engine.get_external_deposits(missing_ledger), 0.0)
        self.assertEqual(analytics_engine.get_external_withdrawals(missing_ledger), 0.0)
        self.assertEqual(analytics_engine.get_net_external_flows(missing_ledger), 0.0)
        self.assertEqual(analytics_engine.get_adjusted_equity(50, missing_ledger), 50.0)
        self.assertEqual(analytics_engine.get_adjusted_pnl(50, 10, missing_ledger), 40.0)
        self.assertEqual(analytics_engine.get_adjusted_roi(50, 10, missing_ledger), 400.0)

        open(self.ledger, 'w', encoding='utf-8').close()

        self.assertEqual(analytics_engine.get_external_deposits(self.ledger), 0.0)
        self.assertEqual(analytics_engine.get_trading_equity(25, self.ledger), 25.0)

    def test_capital_accounting_external_flows_and_adjusted_metrics(self):
        capital_ledger.register_external_deposit(100, ledger_file=self.ledger)
        capital_ledger.register_external_deposit(50, ledger_file=self.ledger)
        capital_ledger.register_external_withdrawal(20, ledger_file=self.ledger)

        self.assertEqual(analytics_engine.get_external_deposits(self.ledger), 150.0)
        self.assertEqual(analytics_engine.get_external_withdrawals(self.ledger), 20.0)
        self.assertEqual(analytics_engine.get_net_external_flows(self.ledger), 130.0)
        self.assertEqual(analytics_engine.get_adjusted_equity(300, self.ledger), 170.0)
        self.assertEqual(analytics_engine.get_trading_equity(300, self.ledger), 170.0)
        self.assertEqual(analytics_engine.get_adjusted_pnl(300, 100, self.ledger), 70.0)
        self.assertEqual(analytics_engine.get_adjusted_roi(300, 100, self.ledger), 70.0)

    def test_capital_accounting_only_deposits(self):
        capital_ledger.register_external_deposit(40, ledger_file=self.ledger)
        capital_ledger.register_external_deposit(10, ledger_file=self.ledger)

        self.assertEqual(analytics_engine.get_external_deposits(self.ledger), 50.0)
        self.assertEqual(analytics_engine.get_external_withdrawals(self.ledger), 0.0)
        self.assertEqual(analytics_engine.get_net_external_flows(self.ledger), 50.0)
        self.assertEqual(analytics_engine.get_adjusted_equity(75, self.ledger), 25.0)

    def test_capital_accounting_only_withdrawals(self):
        capital_ledger.register_external_withdrawal(15, ledger_file=self.ledger)
        capital_ledger.register_external_withdrawal(5, ledger_file=self.ledger)

        self.assertEqual(analytics_engine.get_external_deposits(self.ledger), 0.0)
        self.assertEqual(analytics_engine.get_external_withdrawals(self.ledger), 20.0)
        self.assertEqual(analytics_engine.get_net_external_flows(self.ledger), -20.0)
        self.assertEqual(analytics_engine.get_adjusted_equity(75, self.ledger), 95.0)

    def test_capital_accounting_commissions_funding_and_realized_pnl(self):
        capital_ledger.register_commission(0.25, ledger_file=self.ledger)
        capital_ledger.register_funding_fee(-0.1, ledger_file=self.ledger)
        capital_ledger.register_realized_pnl(4.75, ledger_file=self.ledger)

        summary = analytics_engine.get_capital_accounting_stats(50, 40, self.ledger)

        self.assertEqual(summary['commissions'], 0.25)
        self.assertEqual(summary['funding'], -0.1)
        self.assertEqual(summary['realized_trading_pnl'], 4.75)
        self.assertEqual(summary['adjusted_equity'], 50.0)
        self.assertEqual(summary['adjusted_pnl'], 10.0)
        self.assertEqual(summary['adjusted_roi'], 25.0)

    def _live_observation(self, spot=1.5, futures=-0.5, errors=None):
        total = None if errors else spot + futures
        return {
            'timestamp': '2026-07-21T00:00:00Z',
            'observation_source': 'test_read_only',
            'observed_equity': 101.0,
            'baseline_spot_unrealized_pnl': None if errors else spot,
            'baseline_futures_unrealized_pnl': futures,
            'baseline_unrealized_pnl': total,
            'open_positions_at_bootstrap': [
                {'symbol': 'XRPUSDT', 'wallet': 'SPOT', 'quantity': 10, 'entry_price': 1, 'current_price': 1 + spot / 10, 'unrealized_pnl': spot},
                {'symbol': 'BTCUSDT', 'wallet': 'FUTURES', 'unrealized_pnl': futures},
            ],
            'errors': errors or [],
        }

    def test_live_accounting_combines_spot_and_futures_upnl(self):
        capital_ledger.record_movement(capital_ledger.TYPE_INITIAL_CAPITAL, 100, reference_id='initial', ledger_file=self.ledger)
        summary = analytics_engine.get_live_capital_accounting_stats(
            self.ledger, observer=lambda: self._live_observation(2.0, -1.0))
        self.assertTrue(summary['observation_complete'])
        self.assertEqual(summary['current_spot_unrealized_pnl'], 2.0)
        self.assertEqual(summary['current_futures_unrealized_pnl'], -1.0)
        self.assertEqual(summary['current_unrealized_pnl'], 1.0)
        self.assertEqual(len(summary['current_unrealized_pnl_by_position']), 2)
        self.assertIn(summary['accounting_status'], ('ALIGNED', 'WITHIN_TOLERANCE'))

    def test_live_accounting_supports_negative_spot_upnl(self):
        summary = analytics_engine.get_live_capital_accounting_stats(
            self.ledger, observer=lambda: self._live_observation(-2.0, 0.0))
        self.assertEqual(summary['current_spot_unrealized_pnl'], -2.0)
        self.assertEqual(summary['current_unrealized_pnl'], -2.0)

    def test_live_accounting_mismatch_or_missing_price_is_incomplete_not_zero(self):
        for error in ('managed_spot_quantity_mismatch:XRPUSDT', 'missing_current_price:XRPUSDT'):
            summary = analytics_engine.get_live_capital_accounting_stats(
                self.ledger, observer=lambda e=error: self._live_observation(errors=[e]))
            self.assertFalse(summary['observation_complete'])
            self.assertIsNone(summary['current_unrealized_pnl'])
            self.assertEqual(summary['accounting_status'], 'INCOMPLETE_DATA')
            self.assertIn(error, summary['missing_fields'])

    def test_capital_accounting_does_not_change_existing_analytics_metrics(self):
        self._seed_closed_trades()
        baseline = self._rebuild()
        baseline_general = dict(baseline['general'])

        capital_ledger.register_external_deposit(1000, ledger_file=self.ledger)
        capital_ledger.register_external_withdrawal(250, ledger_file=self.ledger)
        capital_ledger.register_commission(1.5, ledger_file=self.ledger)
        after = self._rebuild()
        after_general = after['general']

        for key in (
            'total_trades',
            'open_trades',
            'closed_trades',
            'win_rate',
            'profit_factor',
            'expectancy',
            'pnl_total',
            'pnl_average',
            'max_drawdown_usdt',
        ):
            self.assertEqual(after_general[key], baseline_general[key])


    def test_bot_version_metrics_use_opening_version_and_keep_general_stats(self):
        rows = [
            {'event_type': 'TRADE_OPEN', 'trade_id': 'v1-win', 'status': 'OPEN', 'opened_at': '2026-01-01T00:00:00Z', 'bot_version': 'v1'},
            {'event_type': 'TRADE_CLOSE', 'trade_id': 'v1-win', 'status': 'CLOSED', 'closed_at': '2026-01-02T00:00:00Z', 'pnl_usdt': 10, 'bot_version': 'v2'},
            {'event_type': 'TRADE_OPEN', 'trade_id': 'v1-loss', 'status': 'OPEN', 'opened_at': '2026-01-03T00:00:00Z', 'bot_version': 'v1'},
            {'event_type': 'TRADE_CLOSE', 'trade_id': 'v1-loss', 'status': 'CLOSED', 'closed_at': '2026-01-04T00:00:00Z', 'pnl_usdt': -5, 'bot_version': 'v2'},
            {'event_type': 'TRADE_OPEN', 'trade_id': 'v2-open', 'status': 'OPEN', 'opened_at': '2026-01-05T00:00:00Z', 'bot_version': 'v2'},
        ]
        for row in rows:
            self.store._append(self.trades, row)

        stats = self._rebuild()
        v1 = stats['by_bot_version']['v1']
        v2 = stats['by_bot_version']['v2']

        self.assertEqual((v1['trades'], v1['open'], v1['closed']), (2, 0, 2))
        self.assertEqual((v1['win'], v1['loss'], v1['win_rate']), (1, 1, 50.0))
        self.assertEqual((v1['pnl_total'], v1['profit_factor'], v1['expectancy']), (5.0, 2.0, 2.5))
        self.assertEqual((v1['first_trade'], v1['last_trade']), ('2026-01-01T00:00:00Z', '2026-01-03T00:00:00Z'))
        self.assertEqual((v2['trades'], v2['open'], v2['closed']), (1, 1, 0))
        self.assertEqual((stats['general']['trades'], stats['general']['closed'], stats['general']['pnl_total']), (3, 2, 5.0))

    def test_missing_opening_bot_version_is_legacy_unknown(self):
        self.store._append(self.trades, {
            'event_type': 'TRADE_OPEN', 'trade_id': 'legacy', 'status': 'OPEN',
            'opened_at': '2026-01-01T00:00:00Z',
        })
        self.store._append(self.trades, {
            'event_type': 'TRADE_CLOSE', 'trade_id': 'legacy', 'status': 'CLOSED',
            'closed_at': '2026-01-02T00:00:00Z', 'pnl_usdt': 1, 'bot_version': 'v2',
        })

        stats = self._rebuild()

        self.assertEqual(stats['by_bot_version']['legacy/unknown']['closed'], 1)
        self.assertNotIn('v2', stats['by_bot_version'])

    def test_current_bot_version_is_present_without_trades(self):
        stats = self._rebuild()
        current = analytics_engine.version_history.current_version()

        self.assertIn(current, stats['by_bot_version'])
        self.assertEqual(stats['by_bot_version'][current]['trades'], 0)
        self.assertEqual(stats['by_bot_version'][current]['open'], 0)
        self.assertEqual(stats['by_bot_version'][current]['closed'], 0)


    def test_version_diagnostic_breakdowns_concentration_and_sizing(self):
        rows = [
            {'event_type': 'TRADE_OPEN', 'trade_id': 'long-a', 'status': 'OPEN', 'symbol': 'AAA', 'side': 'LONG', 'opened_at': '2026-01-01T00:00:00Z', 'bot_version': 'diag', 'regime': 'bullish', 'capital_used': 10},
            {'event_type': 'TRADE_CLOSE', 'trade_id': 'long-a', 'status': 'CLOSED', 'symbol': 'AAA', 'side': 'LONG', 'closed_at': '2026-01-01T01:00:00Z', 'bot_version': 'new', 'exit_reason': 'TP', 'pnl_usdt': 4},
            {'event_type': 'TRADE_OPEN', 'trade_id': 'short-b', 'status': 'OPEN', 'symbol': 'BBB', 'side': 'SHORT', 'opened_at': '2026-01-02T00:00:00Z', 'bot_version': 'diag', 'regime': 'sideways', 'capital_used': 20},
            {'event_type': 'TRADE_CLOSE', 'trade_id': 'short-b', 'status': 'CLOSED', 'symbol': 'BBB', 'side': 'SHORT', 'closed_at': '2026-01-02T01:00:00Z', 'exit_reason': 'SL', 'pnl_usdt': -6},
            {'event_type': 'TRADE_OPEN', 'trade_id': 'long-c', 'status': 'OPEN', 'symbol': 'CCC', 'side': 'LONG', 'opened_at': '2026-01-03T00:00:00Z', 'bot_version': 'diag', 'regime': 'bear', 'capital_used': 30},
            {'event_type': 'TRADE_CLOSE', 'trade_id': 'long-c', 'status': 'CLOSED', 'symbol': 'CCC', 'side': 'LONG', 'closed_at': '2026-01-03T01:00:00Z', 'exit_reason': 'STALE', 'pnl_usdt': -2},
        ]
        for row in rows:
            self.store._append(self.trades, row)

        report = analytics_engine.analyze_version_performance('diag', self.trades)

        self.assertEqual((report['summary']['trades'], report['summary']['closed']), (3, 3))
        self.assertEqual(report['by_side']['LONG']['closed'], 2)
        self.assertEqual(report['by_side']['SHORT']['pnl_total'], -6)
        self.assertEqual(report['by_regime']['BULL']['closed'], 1)
        self.assertEqual(report['by_regime']['NEUTRAL']['closed'], 1)
        self.assertEqual(report['by_exit_reason']['PREVENTIVE']['closed'], 1)
        self.assertEqual(report['by_exit_reason']['SL']['closed_percent'], 33.3333)
        self.assertEqual(report['symbol_ranking'][0]['symbol'], 'BBB')
        self.assertEqual(report['concentration']['largest_loss_side'], 'SHORT')
        self.assertEqual(report['sizing']['distribution'], {'SMALL': 1, 'MEDIUM': 1, 'LARGE': 1})
        self.assertIn('LOW_SAMPLE', report['flags'])
        self.assertIn('NEGATIVE_EXPECTANCY', report['flags'])

    def test_partial_close_inherits_opening_version_regime_and_capital(self):
        self.store._append(self.trades, {
            'event_type': 'TRADE_OPEN', 'trade_id': 'base', 'status': 'OPEN',
            'symbol': 'AAA', 'side': 'LONG', 'bot_version': 'diag',
            'regime': 'bull', 'capital_used': 25, 'opened_at': '2026-01-01T00:00:00Z',
        })
        self.store._append(self.trades, {
            'event_type': 'TRADE_CLOSE', 'trade_id': 'base:partial', 'status': 'CLOSED',
            'symbol': 'AAA', 'side': 'LONG', 'exit_reason': 'PARTIAL', 'pnl_usdt': 1,
            'closed_at': '2026-01-01T01:00:00Z', 'bot_version': 'later',
        })

        report = analytics_engine.analyze_version_performance('diag', self.trades)

        self.assertEqual(report['summary']['trades'], 2)
        self.assertEqual(report['by_regime']['BULL']['trades'], 2)
        self.assertEqual(report['sizing']['sample_size'], 2)
        self.assertEqual(report['by_exit_reason']['OTHER_UNKNOWN']['closed'], 1)


if __name__ == '__main__':
    unittest.main()
