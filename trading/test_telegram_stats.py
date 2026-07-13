#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import unittest
import inspect
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import telegram_commands


def sample_stats():
    return {
        'general': {
            'total_trades': 3,
            'open_trades': 1,
            'closed_trades': 2,
            'win': 1,
            'loss': 1,
            'breakeven': 0,
            'win_rate': 50.0,
            'profit_factor': 2.0,
            'expectancy': 2.5,
            'pnl_total': 5.0,
            'duration_average_minutes': 60.0,
            'best_trade': {'symbol': 'ETHUSDT', 'pnl_usdt': 10, 'pnl_pct': 10},
            'worst_trade': {'symbol': 'BTCUSDT', 'pnl_usdt': -5, 'pnl_pct': -5},
            'max_drawdown_usdt': 5,
            'pnl_daily': {},
            'pnl_weekly': {},
            'pnl_monthly': {},
        },
        'symbol_ranking': [
            {'symbol': 'ETHUSDT', 'trades': 2, 'closed': 1, 'win_rate': 100, 'pnl_total': 10, 'profit_factor': None, 'expectancy': 10},
            {'symbol': 'BTCUSDT', 'trades': 1, 'closed': 1, 'win_rate': 0, 'pnl_total': -5, 'profit_factor': 0, 'expectancy': -5},
        ],
        'by_direction': {
            'LONG': {'trades': 2, 'closed': 1, 'win_rate': 100, 'pnl_total': 10, 'profit_factor': None, 'expectancy': 10, 'duration_average_minutes': 60},
            'SHORT': {'trades': 1, 'closed': 1, 'win_rate': 0, 'pnl_total': -5, 'profit_factor': 0, 'expectancy': -5, 'duration_average_minutes': 30},
        },
        'by_regime': {
            'bull': {'trades': 1, 'win_rate': 100, 'pnl_total': 10},
            'bear': {'trades': 1, 'win_rate': 0, 'pnl_total': -5},
            'sideways': {'trades': 0, 'win_rate': 0, 'pnl_total': 0},
            'neutral': {'trades': 0, 'win_rate': 0, 'pnl_total': 0},
            'unknown': {'trades': 1, 'win_rate': 0, 'pnl_total': 0},
        },
        'by_exit_reason': {
            'TP': {'closed': 1},
            'SL': {'closed': 1},
            'TRAILING': {'closed': 0},
            'PARTIAL': {'closed': 0},
            'RECOVERY': {'closed': 0},
            'EMERGENCY': {'closed': 0},
            'MANUAL': {'closed': 0},
            'STALE': {'closed': 0},
        },
        'time': {
            'hour': {'01': {'closed': 1, 'win_rate': 100, 'pnl_total': 10}},
            'day': {'2026-01-01': {'closed': 1, 'win_rate': 100, 'pnl_total': 10}},
            'week': {'2026-W01': {'closed': 2, 'win_rate': 50, 'pnl_total': 5}},
            'month': {'2026-01': {'closed': 2, 'win_rate': 50, 'pnl_total': 5}},
        },
        'history': {
            'trades_registered': 5,
            'snapshots_registered': 2,
            'decisions_registered': 8,
            'first_record': '2026-01-01T00:00:00Z',
            'last_record': '2026-01-02T00:00:00Z',
        },
    }


class TelegramStatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stats_file = os.path.join(self.tmp.name, 'stats.json')
        self._old_daily_pnl_paths = telegram_commands.DAILY_PNL_FALLBACK_PATHS
        self._old_daily_pnl_now = telegram_commands.DAILY_PNL_FALLBACK_NOW

    def tearDown(self):
        telegram_commands.DAILY_PNL_FALLBACK_PATHS = self._old_daily_pnl_paths
        telegram_commands.DAILY_PNL_FALLBACK_NOW = self._old_daily_pnl_now
        self.tmp.cleanup()

    def _write_jsonl(self, name, rows):
        path = os.path.join(self.tmp.name, name)
        with open(path, 'w', encoding='utf-8') as f:
            for row in rows:
                f.write(json.dumps(row) + '\n')
        return path

    def _patch_stats(self, stats=None):
        return patch.multiple(
            telegram_commands.analytics_engine,
            DEFAULT_STATS_FILE=self.stats_file,
            load_stats=lambda: stats or sample_stats(),
        )

    def _metrics(self, long_count=0, short_count=0):
        return {
            'long_count': long_count,
            'short_count': short_count,
            'max_longs': 2,
            'max_shorts': 2,
            'total_real': 54.0,
            'total_limit': 54.0,
            'total_authorized': 54.0,
            'spot_real': 26.9,
            'spot_target': 26.9,
            'spot_used': 8.4,
            'spot_reserved': 0,
            'futures_real': 27.1,
            'futures_target': 27.1,
            'futures_used': 18.2,
            'futures_reserved': 0,
            'rebalance': {},
            'max_exposure_percent': 80.0,
            'max_position_percent': None,
            'warning': None,
            'note': None,
        }

    def _accounting(self, **overrides):
        data = {
            'external_deposits': 0.0,
            'external_withdrawals': 0.0,
            'net_external_flows': 0.0,
            'commissions': 0.0,
            'funding': 0.0,
            'realized_trading_pnl': 0.0,
            'adjusted_equity': 54.0,
            'adjusted_pnl': 0.0,
            'adjusted_roi': 0.0,
        }
        data.update(overrides)
        return data

    def test_stats_general_format(self):
        with self._patch_stats(), \
             patch.object(telegram_commands, '_exposure_metrics', return_value=self._metrics(long_count=1)), \
             patch.object(telegram_commands, '_bot_state', return_value={}):
            response = telegram_commands._render_page('stats_general')

        text = response['text']
        self.assertIn('Resumen General', text)
        self.assertIn('Operacion:', text)
        self.assertIn('PnL:', text)
        self.assertIn('Capital:', text)
        self.assertIn('Trading ajustado:', text)
        self.assertIn('Trades totales: 3', text)
        self.assertIn('Win Rate: 50.0%', text)
        self.assertIn('Profit Factor: 2.00', text)
        self.assertIn('Total: +5.00 USDT', text)

    def test_stats_symbols_format(self):
        with self._patch_stats():
            response = telegram_commands._render_page('stats_symbols')

        text = response['text']
        self.assertIn('Por simbolo', text)
        self.assertLess(text.find('ETHUSDT'), text.find('BTCUSDT'))
        self.assertIn('PF N/A', text)

    def test_stats_missing_file_warns_and_uses_engine(self):
        with self._patch_stats(), \
             patch.object(telegram_commands, '_exposure_metrics', return_value=self._metrics(long_count=1)), \
             patch.object(telegram_commands, '_bot_state', return_value={}):
            response = telegram_commands._render_page('stats')

        self.assertIn('Stats no existia; reconstruido desde historial.', response['text'])
        self.assertIn('Trades: 3', response['text'])

    def test_stats_corrupt_file_warns_and_uses_engine(self):
        with open(self.stats_file, 'w', encoding='utf-8') as f:
            f.write('{invalid json')

        with self._patch_stats():
            response = telegram_commands._render_page('stats_history')

        self.assertIn('WARNING: stats.json corrupto', response['text'])
        self.assertIn('Trades registrados: 5', response['text'])

    def test_stats_exit_and_time_formats(self):
        with self._patch_stats():
            exits = telegram_commands._render_page('stats_exits')['text']
            temporal = telegram_commands._render_page('stats_time')['text']

        self.assertIn('TP: 1 | 50.0%', exits)
        self.assertIn('SL: 1 | 50.0%', exits)
        self.assertIn('2026-01-01: +10.00 USDT', temporal)

    def test_stats_regimes_uses_canonical_labels(self):
        with self._patch_stats():
            text = telegram_commands._render_page('stats_regimes')['text']

        self.assertIn('Bull: Trades 1', text)
        self.assertIn('Bear: Trades 1', text)
        self.assertIn('Sideways: Trades 0', text)
        self.assertIn('Neutral: Trades 0', text)
        self.assertIn('Unknown: Trades 1', text)

    def test_split_text_keeps_long_messages(self):
        chunks = telegram_commands._split_text('a\n' * 5000, limit=1000)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 1000 for chunk in chunks))

    def test_home_uses_analytics_pnl_and_real_wallet_totals(self):
        stats = sample_stats()
        stats['general']['pnl_total'] = 12.34
        today = telegram_commands.datetime.now(telegram_commands.UY_TZ).date().isoformat()
        stats['general']['pnl_daily'] = {today: 1.23}
        bot_snapshot = {
            'system': {'health': 'OK', 'last_execution': '2026-01-01T12:00:00Z'},
            'pnl': {'today': -0.4402, 'total': 5.0022},
            'market': {'regime': 'bearish', 'btc_change_4h': -1.23, 'btc_price': 61234.56, 'directional_mode': True},
            'capital': {
                'spot_real': 26.9,
                'spot_target': 25.0,
                'spot_used': 8.4,
                'futures_real': 27.1,
                'futures_target': 30.0,
                'futures_used': 18.2,
            },
            'positions': {
                'long': {'current': 1, 'max': 2},
                'short': {'current': 1, 'max': 2},
            },
        }

        with patch.object(telegram_commands, '_stats_payload', return_value=(stats, None)), \
             patch.object(telegram_commands, '_bot_state', return_value=bot_snapshot), \
             patch.object(telegram_commands, '_state', return_value={'daily_pnl_usdt': 0, 'total_pnl_usdt': 0}), \
             patch.object(telegram_commands, '_health_summary', return_value=('OK', [], [])), \
             patch.object(telegram_commands, '_bot_status', return_value='ONLINE'), \
             patch.object(telegram_commands, '_guardian_status', return_value='ONLINE'):
            text = telegram_commands._render_page('home')['text']

        self.assertIn('Hoy: +1.23 USDT', text)
        self.assertIn('Total: +12.34 USDT', text)
        self.assertIn('Bear | BTC 4h -1.23%', text)
        self.assertIn('BTC: $61,234.56', text)
        self.assertIn('📈 Longs: 1/2', text)
        self.assertIn('Spot: 8.40 USDT / 26.90 USDT', text)
        self.assertIn('📉 Shorts: 1/2', text)
        self.assertIn('Futures margen: 18.20 USDT / 27.10 USDT', text)
        self.assertIn('Ultimo ciclo: 09:00 UY', text)
        self.assertNotIn('Spot: 8.40 USDT / 25.00 USDT', text)
        self.assertNotIn('Futures margen: 18.20 USDT / 30.00 USDT', text)
        self.assertNotIn('01/01 09:00 UY', text)

    def test_stats_and_home_use_same_analytics_pnl_source(self):
        stats = sample_stats()
        stats['general']['pnl_total'] = 12.34
        today = telegram_commands.datetime.now(telegram_commands.UY_TZ).date().isoformat()
        stats['general']['pnl_daily'] = {today: 1.23}
        bot_snapshot = {
            'system': {'health': 'OK', 'last_execution': '2026-01-01T12:00:00Z'},
            'pnl': {'today': -0.44, 'total': 5.0},
            'capital': {'spot_real': 26.9, 'spot_used': 8.4, 'futures_real': 27.1, 'futures_used': 18.2},
            'positions': {'long': {'current': 1, 'max': 2}, 'short': {'current': 1, 'max': 2}},
        }
        with patch.object(telegram_commands, '_stats_payload', return_value=(stats, None)), \
             patch.object(telegram_commands, '_bot_state', return_value=bot_snapshot), \
             patch.object(telegram_commands, '_state', return_value={}), \
             patch.object(telegram_commands, '_health_summary', return_value=('OK', [], [])), \
             patch.object(telegram_commands, '_bot_status', return_value='ONLINE'), \
             patch.object(telegram_commands, '_guardian_status', return_value='ONLINE'), \
             patch.object(telegram_commands, '_exposure_metrics', return_value=self._metrics()):
            home = telegram_commands._render_page('home')['text']
            stats_text = telegram_commands._render_page('stats')['text']

        self.assertIn('Hoy: +1.23 USDT', home)
        self.assertIn('Total: +12.34 USDT', home)
        self.assertIn('Hoy: +1.23 USDT', stats_text)
        self.assertIn('Total: +12.34 USDT', stats_text)
        self.assertNotIn('-0.44 USDT', home)
        self.assertNotIn('+5.00 USDT', home)

    def test_home_and_stats_fallback_today_pnl_from_closed_trades(self):
        stats = sample_stats()
        stats['general']['pnl_total'] = 12.34
        stats['general']['pnl_daily'] = {}
        now_uy = telegram_commands.datetime(2026, 7, 13, 12, 0, tzinfo=telegram_commands.UY_TZ)
        history_path = self._write_jsonl('fallback_trades.jsonl', [
            {'status': 'CLOSED', 'exit_time': '2026-07-13T10:00:00-03:00', 'pnl_usdt': -0.10},
            {'event_type': 'TRADE_CLOSE', 'closed_at': '2026-07-13T11:00:00-03:00', 'pnl_usdt': -0.20},
            {'status': 'CLOSED', 'exit_time': '2026-07-13T12:00:00-03:00', 'pnl_usdt': -0.12},
            {'status': 'OPEN', 'exit_time': '2026-07-13T12:00:00-03:00', 'pnl_usdt': 99},
            {'status': 'CLOSED', 'exit_time': '2026-07-13T12:00:00-03:00', 'pnl_usdt': 'bad'},
            {'status': 'CLOSED', 'exit_time': '2026-07-12T23:00:00-03:00', 'pnl_usdt': -5},
        ])
        telegram_commands.DAILY_PNL_FALLBACK_PATHS = (history_path,)
        telegram_commands.DAILY_PNL_FALLBACK_NOW = now_uy
        bot_snapshot = {
            'system': {'health': 'OK', 'last_execution': '2026-01-01T12:00:00Z'},
            'capital': {'spot_real': 26.9, 'spot_used': 8.4, 'futures_real': 27.1, 'futures_used': 18.2},
            'positions': {'long': {'current': 1, 'max': 2}, 'short': {'current': 1, 'max': 2}},
        }

        with patch.object(telegram_commands, '_stats_payload', return_value=(stats, None)), \
             patch.object(telegram_commands, '_bot_state', return_value=bot_snapshot), \
             patch.object(telegram_commands, '_state', return_value={}), \
             patch.object(telegram_commands, '_health_summary', return_value=('OK', [], [])), \
             patch.object(telegram_commands, '_bot_status', return_value='ONLINE'), \
             patch.object(telegram_commands, '_guardian_status', return_value='ONLINE'), \
             patch.object(telegram_commands, '_exposure_metrics', return_value=self._metrics()):
            home = telegram_commands._render_page('home')['text']
            stats_text = telegram_commands._render_page('stats')['text']

        self.assertIn('Hoy: -0.42 USDT', home)
        self.assertIn('Hoy: -0.42 USDT', stats_text)
        self.assertIn('Total: +12.34 USDT', home)

    def test_daily_pnl_fallback_does_not_read_productive_files_in_tests_without_explicit_source(self):
        with patch.object(telegram_commands, '_read_jsonl') as read_jsonl:
            pnl = telegram_commands._fallback_today_pnl_from_closed_trades()

        self.assertIsNone(pnl)
        read_jsonl.assert_not_called()

    def test_home_pnl_falls_back_to_bot_state_when_analytics_unavailable(self):
        bot_snapshot = {
            'system': {'health': 'OK', 'last_execution': '2026-01-01T12:00:00Z'},
            'pnl': {'today': -0.4402, 'total': 5.0022},
            'capital': {'spot_real': 26.9, 'spot_used': 8.4, 'futures_real': 27.1, 'futures_used': 18.2},
            'positions': {'long': {'current': 1, 'max': 2}, 'short': {'current': 1, 'max': 2}},
        }

        with patch.object(telegram_commands, '_stats_payload', side_effect=RuntimeError('stats unavailable')), \
             patch.object(telegram_commands, '_bot_state', return_value=bot_snapshot), \
             patch.object(telegram_commands, '_state', return_value={}), \
             patch.object(telegram_commands, '_health_summary', return_value=('OK', [], [])), \
             patch.object(telegram_commands, '_bot_status', return_value='ONLINE'), \
             patch.object(telegram_commands, '_guardian_status', return_value='ONLINE'):
            text = telegram_commands._render_page('home')['text']

        self.assertIn('Hoy: -0.44 USDT', text)
        self.assertIn('Total: +5.00 USDT', text)

    def test_home_pnl_shows_na_when_no_reliable_source_exists(self):
        bot_snapshot = {
            'system': {'health': 'OK', 'last_execution': '2026-01-01T12:00:00Z'},
            'capital': {'spot_real': 26.9, 'spot_used': 8.4, 'futures_real': 27.1, 'futures_used': 18.2},
            'positions': {'long': {'current': 1, 'max': 2}, 'short': {'current': 1, 'max': 2}},
        }

        with patch.object(telegram_commands, '_stats_payload', side_effect=RuntimeError('stats unavailable')), \
             patch.object(telegram_commands, '_bot_state', return_value=bot_snapshot), \
             patch.object(telegram_commands, '_state', return_value={}), \
             patch.object(telegram_commands, '_health_summary', return_value=('OK', [], [])), \
             patch.object(telegram_commands, '_bot_status', return_value='ONLINE'), \
             patch.object(telegram_commands, '_guardian_status', return_value='ONLINE'):
            text = telegram_commands._render_page('home')['text']

        self.assertIn('Hoy: N/A', text)
        self.assertIn('Total: N/A', text)

    def test_capital_market_regime_fallback_when_missing(self):
        with patch.object(telegram_commands, '_exposure_metrics', return_value=self._metrics()), \
             patch.object(telegram_commands, '_bot_state', return_value={}):
            text = telegram_commands._render_page('capital')['text']

        self.assertIn('Mercado:', text)
        self.assertIn('Régimen actual: Unknown', text)
        self.assertIn('BTC 4h: No disponible', text)
        self.assertIn('BTC precio: No disponible', text)
        self.assertIn('Modo direccional: No disponible', text)

    def test_capital_shows_current_market_regime(self):
        snapshot = {
            'market': {
                'regime': 'bullish',
                'btc_change_4h': 2.5,
                'btc_price': 70000,
                'directional_mode': False,
            }
        }
        with patch.object(telegram_commands, '_exposure_metrics', return_value=self._metrics()), \
             patch.object(telegram_commands, '_bot_state', return_value=snapshot):
            text = telegram_commands._render_page('capital')['text']

        self.assertIn('Régimen actual: Bull', text)
        self.assertIn('BTC 4h: +2.50%', text)
        self.assertIn('BTC precio: $70,000.00', text)
        self.assertIn('Modo direccional: Inactivo', text)

    def test_capital_groups_real_used_free_and_rebalance_targets(self):
        metrics = self._metrics()
        metrics['rebalance'] = {'status': 'NOT_REQUIRED', 'direction': 'NONE', 'amount_pending': 0.0}
        with patch.object(telegram_commands, '_exposure_metrics', return_value=metrics), \
             patch.object(telegram_commands, '_bot_state', return_value={}):
            text = telegram_commands._render_page('capital')['text']

        self.assertIn('Total:\nReal: 54.00 USDT\nUsado: 26.60 USDT\nLibre: 27.40 USDT', text)
        self.assertIn('Spot:\nReal: 26.90 USDT\nUsado: 8.40 USDT\nLibre: 18.50 USDT', text)
        self.assertIn('Futures:\nReal: 27.10 USDT\nMargen usado: 18.20 USDT\nLibre: 8.90 USDT', text)
        self.assertIn('Objetivo/Rebalance:', text)
        self.assertIn('Spot objetivo: 26.90 USDT', text)
        self.assertIn('Futures objetivo: 27.10 USDT', text)
        self.assertNotIn('Spot:\nReal: 26.90 USDT\nObjetivo:', text)
        self.assertNotIn('Futures:\nReal: 27.10 USDT\nObjetivo:', text)

    def test_capital_shows_capital_accounting_metrics(self):
        metrics = self._metrics()
        metrics['capital_accounting_starting_equity'] = 129.0
        with patch.object(telegram_commands, '_exposure_metrics', return_value=metrics), \
             patch.object(telegram_commands, '_bot_state', return_value={}), \
             patch.object(
                 telegram_commands.analytics_engine,
                 'get_capital_accounting_stats',
                 return_value=self._accounting(
                     external_deposits=100,
                     external_withdrawals=25,
                     net_external_flows=75,
                     adjusted_equity=-21,
                     adjusted_pnl=-75,
                     adjusted_roi=-138.888,
                 ),
             ):
            text = telegram_commands._render_page('capital')['text']

        self.assertIn('Contabilidad:', text)
        self.assertIn('Depositos externos: 100.00 USDT', text)
        self.assertIn('Retiros externos: 25.00 USDT', text)
        self.assertIn('Flujo externo neto: 75.00 USDT', text)
        self.assertIn('Equity ajustado: -21.00 USDT', text)
        self.assertIn('PnL ajustado: -75.00 USDT', text)
        self.assertIn('ROI ajustado: -138.89%', text)

    def test_capital_accounting_is_safe_without_ledger(self):
        with patch.object(telegram_commands, '_exposure_metrics', return_value=self._metrics()), \
             patch.object(telegram_commands, '_bot_state', return_value={}), \
             patch.object(
                 telegram_commands.analytics_engine,
                 'get_capital_accounting_stats',
                 return_value=self._accounting(adjusted_equity=54, adjusted_pnl=0, adjusted_roi=0),
             ):
            text = telegram_commands._render_page('capital')['text']

        self.assertIn('Depositos externos: 0.00 USDT', text)
        self.assertIn('Retiros externos: 0.00 USDT', text)
        self.assertIn('Flujo externo neto: 0.00 USDT', text)

    def test_capital_accounting_unavailable_without_equity_baseline(self):
        metrics = self._metrics()
        metrics['total_real'] = None
        metrics['total_limit'] = None
        with patch.object(telegram_commands, '_exposure_metrics', return_value=metrics), \
             patch.object(telegram_commands, '_bot_state', return_value={}), \
             patch.object(
                 telegram_commands.analytics_engine,
                 'get_capital_accounting_stats',
                 return_value=self._accounting(adjusted_equity=None, adjusted_pnl=None, adjusted_roi=None),
             ):
            text = telegram_commands._render_page('capital')['text']

        self.assertIn('Equity ajustado: No disponible', text)
        self.assertIn('PnL ajustado: No disponible', text)
        self.assertIn('ROI ajustado: No disponible', text)

    def test_stats_general_uses_live_open_positions_count(self):
        stats = sample_stats()
        stats['general']['closed_trades'] = 17
        stats['general']['total_trades'] = 17
        stats['general']['open_trades'] = 0

        with patch.object(telegram_commands, '_stats_payload', return_value=(stats, None)), \
             patch.object(telegram_commands, '_exposure_metrics', return_value=self._metrics(short_count=2)), \
             patch.object(telegram_commands, '_bot_state', return_value={}):
            text = telegram_commands._render_page('stats_general')['text']

        self.assertIn('Trades totales: 19', text)
        self.assertIn('Abiertos: 2', text)
        self.assertIn('Cerrados: 17', text)

    def test_stats_general_zero_live_positions(self):
        stats = sample_stats()
        stats['general']['closed_trades'] = 17
        stats['general']['total_trades'] = 17
        stats['general']['open_trades'] = 4

        with patch.object(telegram_commands, '_stats_payload', return_value=(stats, None)), \
             patch.object(telegram_commands, '_exposure_metrics', return_value=self._metrics()), \
             patch.object(telegram_commands, '_bot_state', return_value={}):
            text = telegram_commands._render_page('stats_general')['text']

        self.assertIn('Trades totales: 17', text)
        self.assertIn('Abiertos: 0', text)
        self.assertIn('Cerrados: 17', text)

    def test_home_and_stats_share_live_positions_source(self):
        stats = sample_stats()
        stats['general']['closed_trades'] = 17
        metrics = self._metrics(short_count=2)

        with patch.object(telegram_commands, '_stats_payload', return_value=(stats, None)), \
             patch.object(telegram_commands, '_exposure_metrics', return_value=metrics), \
             patch.object(telegram_commands, '_bot_state', return_value={'system': {'health': 'OK'}}), \
             patch.object(telegram_commands, '_state', return_value={}), \
             patch.object(telegram_commands, '_health_summary', return_value=('OK', [], [])), \
             patch.object(telegram_commands, '_bot_status', return_value='ONLINE'), \
             patch.object(telegram_commands, '_guardian_status', return_value='ONLINE'):
            home = telegram_commands._render_page('home')['text']
            stats_text = telegram_commands._render_page('stats_general')['text']

        self.assertIn('Shorts: 2/2', home)
        self.assertIn('Abiertos: 2', stats_text)

    def test_home_shows_futures_reconciliation_instead_of_normal_capacity(self):
        metrics = self._metrics(short_count=5)
        metrics['max_shorts'] = 0
        metrics['futures_reconciliation'] = {
            'observed_count': 5,
            'managed_count': 1,
            'unprotected_count': 5,
            'desynced_count': 1,
            'allowed_count': 0,
            'status': 'EXCESO FUTURES',
        }

        with patch.object(telegram_commands, '_exposure_metrics', return_value=metrics), \
             patch.object(telegram_commands, '_bot_state', return_value={'system': {'health': 'OK'}}), \
             patch.object(telegram_commands, '_state', return_value={}), \
             patch.object(telegram_commands, '_health_summary', return_value=('OK', [], [])), \
             patch.object(telegram_commands, '_bot_status', return_value='ONLINE'), \
             patch.object(telegram_commands, '_guardian_status', return_value='ONLINE'):
            home = telegram_commands._render_page('home')['text']

        self.assertIn('Shorts:', home)
        self.assertIn('- Observadas: 5', home)
        self.assertIn('- Gestionadas: 1', home)
        self.assertIn('- Permitidas ahora: 0', home)
        self.assertIn('- Sin proteccion: 5', home)
        self.assertIn('- Estado: EXCESO FUTURES', home)
        self.assertNotIn('Shorts: 5/5', home)

    def test_home_compacts_healthy_futures_reconciliation(self):
        metrics = self._metrics(short_count=0)
        metrics['max_shorts'] = 0
        metrics['futures_used'] = 0.0
        metrics['futures_real'] = 0.10
        metrics['futures_reconciliation'] = {
            'observed_count': 0,
            'managed_count': 0,
            'unmanaged_count': 0,
            'orphan_count': 0,
            'unprotected_count': 0,
            'desynced_count': 0,
            'allowed_count': 2,
            'aligned': True,
            'status': 'ALINEADO',
        }

        with patch.object(telegram_commands, '_exposure_metrics', return_value=metrics), \
             patch.object(telegram_commands, '_bot_state', return_value={'system': {'health': 'OK'}}), \
             patch.object(telegram_commands, '_state', return_value={}), \
             patch.object(telegram_commands, '_health_summary', return_value=('OK', [], [])), \
             patch.object(telegram_commands, '_bot_status', return_value='ONLINE'), \
             patch.object(telegram_commands, '_guardian_status', return_value='ONLINE'):
            home = telegram_commands._render_page('home')['text']

        self.assertIn('Shorts: 0/0', home)
        self.assertNotIn('Shorts: 0/2', home)
        self.assertIn('Futures margen: 0.00 USDT / 0.10 USDT', home)
        self.assertNotIn('- Observadas:', home)
        self.assertNotIn('- Gestionadas:', home)
        self.assertNotIn('- Sin proteccion:', home)
        self.assertNotIn('- Estado:', home)

    def test_home_compacts_managed_futures_even_when_capital_not_aligned(self):
        metrics = self._metrics(short_count=2)
        metrics['max_shorts'] = 2
        metrics['futures_used'] = 17.72
        metrics['futures_real'] = 23.72
        metrics['futures_reconciliation'] = {
            'observed_count': 2,
            'managed_count': 2,
            'unmanaged_count': 0,
            'orphan_count': 0,
            'unprotected_count': 0,
            'desynced_count': 0,
            'allowed_count': 2,
            'aligned': False,
            'status': 'NO ALINEADO',
        }

        with patch.object(telegram_commands, '_exposure_metrics', return_value=metrics), \
             patch.object(telegram_commands, '_bot_state', return_value={'system': {'health': 'OK'}}), \
             patch.object(telegram_commands, '_state', return_value={}), \
             patch.object(telegram_commands, '_health_summary', return_value=('OK', [], [])), \
             patch.object(telegram_commands, '_bot_status', return_value='ONLINE'), \
             patch.object(telegram_commands, '_guardian_status', return_value='ONLINE'):
            home = telegram_commands._render_page('home')['text']

        self.assertIn('Shorts: 2/2', home)
        self.assertIn('Futures margen: 17.72 USDT / 23.72 USDT', home)
        self.assertNotIn('- Observadas:', home)
        self.assertNotIn('- Gestionadas:', home)
        self.assertNotIn('- Permitidas ahora:', home)
        self.assertNotIn('Estado: NO ALINEADO', home)

    def test_system_shows_runtime_version_metadata(self):
        with patch.object(telegram_commands, '_bot_status', return_value='ONLINE'), \
             patch.object(telegram_commands, '_guardian_status', return_value='ONLINE'), \
             patch.object(telegram_commands, '_dashboard_status', return_value='ONLINE'), \
             patch.object(telegram_commands, '_telegram_service_status', return_value='ONLINE'), \
             patch.object(telegram_commands, '_git_commit', return_value='abc123'), \
             patch.object(telegram_commands, '_git_deploy_time', return_value='2026-07-08 00:00 UTC'), \
             patch.object(telegram_commands, '_systemd_active_since', return_value='N/A'), \
             patch.object(telegram_commands, '_server_uptime', return_value='N/A'):
            text = telegram_commands._render_page('system')['text']

        self.assertIn('Bot version: v1.2-sizing-v2', text)
        self.assertIn('Strategy version: current', text)
        self.assertIn('Schema version: v1', text)

    def test_diagnostics_shows_runtime_version_metadata(self):
        with patch.object(telegram_commands, '_bot_state', return_value={'system': {'health': 'OK'}}), \
             patch.object(telegram_commands, '_exposure_metrics', return_value=self._metrics()), \
             patch.object(telegram_commands, '_market_summary_lines', return_value=['Regimen actual: Neutral']):
            text = telegram_commands._render_page('diagnostics')['text']

        self.assertIn('Bot version: v1.2-sizing-v2', text)
        self.assertIn('Strategy version: current', text)
        self.assertIn('Schema version: v1', text)
        self.assertIn('Entradas\n', text)
        self.assertIn('Longs\n', text)
        self.assertIn('Shorts\n', text)
        self.assertIn('Rebalance\n', text)

    def test_home_expands_futures_reconciliation_when_unmanaged(self):
        metrics = self._metrics(short_count=1)
        metrics['futures_reconciliation'] = {
            'observed_count': 1,
            'managed_count': 0,
            'unmanaged_count': 1,
            'orphan_count': 0,
            'unprotected_count': 0,
            'desynced_count': 0,
            'allowed_count': 2,
            'aligned': False,
            'status': 'RIESGO NO GESTIONADAS',
        }

        with patch.object(telegram_commands, '_exposure_metrics', return_value=metrics), \
             patch.object(telegram_commands, '_bot_state', return_value={'system': {'health': 'OK'}}), \
             patch.object(telegram_commands, '_state', return_value={}), \
             patch.object(telegram_commands, '_health_summary', return_value=('OK', [], [])), \
             patch.object(telegram_commands, '_bot_status', return_value='ONLINE'), \
             patch.object(telegram_commands, '_guardian_status', return_value='ONLINE'):
            home = telegram_commands._render_page('home')['text']

        self.assertIn('Shorts:', home)
        self.assertIn('- Observadas: 1', home)
        self.assertIn('- Gestionadas: 0', home)
        self.assertIn('- Permitidas ahora: 2', home)
        self.assertIn('- Estado: RIESGO NO GESTIONADAS', home)

    def test_live_open_positions_do_not_change_historical_stats(self):
        stats = sample_stats()
        stats['general']['closed_trades'] = 17
        stats['general']['win_rate'] = 64.7
        stats['general']['profit_factor'] = 1.85
        stats['general']['expectancy'] = 0.73
        stats['general']['pnl_total'] = 12.34

        with patch.object(telegram_commands, '_stats_payload', return_value=(stats, None)), \
             patch.object(telegram_commands, '_exposure_metrics', return_value=self._metrics(long_count=1, short_count=1)), \
             patch.object(telegram_commands, '_bot_state', return_value={}):
            text = telegram_commands._render_page('stats_general')['text']

        self.assertIn('Win Rate: 64.7%', text)
        self.assertIn('Profit Factor: 1.85', text)
        self.assertIn('Expectancy: +0.73 USDT', text)
        self.assertIn('Total: +12.34 USDT', text)

    def test_stats_adds_capital_accounting_without_replacing_existing_metrics(self):
        stats = sample_stats()
        metrics = self._metrics()
        metrics['capital_accounting_starting_equity'] = 129.0
        with patch.object(telegram_commands, '_stats_payload', return_value=(stats, None)), \
             patch.object(telegram_commands, '_exposure_metrics', return_value=metrics), \
             patch.object(telegram_commands, '_bot_state', return_value={}), \
             patch.object(
                 telegram_commands.analytics_engine,
                 'get_capital_accounting_stats',
                 return_value=self._accounting(
                     net_external_flows=75,
                     commissions=0.5,
                     funding=-0.1,
                     adjusted_pnl=-75,
                     adjusted_roi=-138.89,
                 ),
             ):
            text = telegram_commands._render_page('stats_general')['text']

        self.assertIn('Win Rate: 50.0%', text)
        self.assertIn('Profit Factor: 2.00', text)
        self.assertIn('Total: +5.00 USDT', text)
        self.assertIn('Trading ajustado:', text)
        self.assertIn('PnL Trading: -75.00 USDT', text)
        self.assertIn('ROI Trading: -138.89%', text)
        self.assertIn('Aportes netos: 75.00 USDT', text)
        self.assertIn('Comisiones: 0.50 USDT', text)
        self.assertIn('Funding: -0.10 USDT', text)

    def test_stats_uses_analytics_pnl_total_when_available(self):
        stats = sample_stats()
        stats['general']['pnl_total'] = 3.26
        metrics = self._metrics()
        bot_snapshot = {'pnl': {'today': -0.4402, 'total': 5.0022}}

        with patch.object(telegram_commands, '_stats_payload', return_value=(stats, None)), \
             patch.object(telegram_commands, '_exposure_metrics', return_value=metrics), \
             patch.object(telegram_commands, '_bot_state', return_value=bot_snapshot):
            text = telegram_commands._render_page('stats_general')['text']

        self.assertIn('Total: +3.26 USDT', text)
        self.assertIn('Hoy: N/A', text)
        self.assertNotIn('Total: +5.00 USDT', text)
        self.assertNotIn('Hoy: -0.44 USDT', text)

    def test_stats_does_not_treat_limit_gap_as_trading_loss(self):
        stats = sample_stats()
        metrics = self._metrics()
        metrics['total_real'] = 47.76
        metrics['total_limit'] = 100.0
        metrics['total_authorized'] = 47.76

        with patch.object(telegram_commands, '_stats_payload', return_value=(stats, None)), \
             patch.object(telegram_commands, '_exposure_metrics', return_value=metrics), \
             patch.object(telegram_commands, '_bot_state', return_value={'pnl': {'today': -0.4402, 'total': 5.0022}}), \
             patch.object(
                 telegram_commands.analytics_engine,
                 'get_capital_accounting_stats',
                 return_value=self._accounting(
                     adjusted_equity=47.76,
                     adjusted_pnl=-52.24,
                     adjusted_roi=-52.24,
                 ),
             ):
            text = telegram_commands._render_page('stats_general')['text']

        self.assertIn('Real: 47.76 USDT', text)
        self.assertIn('Limite: 100.00 USDT', text)
        self.assertIn('Autorizado: 47.76 USDT', text)
        self.assertIn('PnL Trading: No disponible', text)
        self.assertIn('ROI Trading: No disponible', text)
        self.assertIn('Motivo: faltan aportes/retiros/base inicial confiable', text)
        self.assertNotIn('PnL Trading: -52.24 USDT', text)
        self.assertNotIn('ROI Trading: -52.24%', text)

    def test_telegram_uses_analytics_for_capital_accounting_only(self):
        source = inspect.getsource(telegram_commands)

        self.assertIn('analytics_engine.get_capital_accounting_stats', source)
        self.assertNotIn('import capital_ledger', source)
        self.assertNotIn('import capital_accounting', source)

    def test_futures_recovery_preview_command_is_read_only(self):
        preview = {'candidates': []}
        with patch.object(telegram_commands.futures_recovery, 'preview_recovery', return_value=preview) as preview_fn, \
             patch.object(telegram_commands.futures_recovery, 'format_preview_text', return_value='preview') as format_fn, \
             patch.object(telegram_commands.futures_recovery, 'close_position') as close_position:
            response = telegram_commands._dispatch_text('/futures_recovery_preview')

        self.assertEqual(response['text'], 'preview')
        preview_fn.assert_called_once_with()
        format_fn.assert_called_once_with(preview)
        close_position.assert_not_called()

    def test_futures_recovery_close_command_passes_confirm_literal(self):
        result = {'ok': False, 'reason': 'missing_confirm', 'symbol': 'NEARUSDT'}
        with patch.object(telegram_commands.futures_recovery, 'close_position', return_value=result) as close_position, \
             patch.object(telegram_commands.futures_recovery, 'format_close_result', return_value='result') as format_result:
            response = telegram_commands._dispatch_text('/futures_recovery_close NEARUSDT CONFIRM')

        self.assertEqual(response['text'], 'result')
        close_position.assert_called_once_with('NEARUSDT', confirm='CONFIRM')
        format_result.assert_called_once_with(result)

    def test_insights_low_sample_suppresses_misleading_comparisons(self):
        stats = sample_stats()
        stats['general']['closed_trades'] = 1
        stats['general']['closed'] = 1
        stats['by_symbol'] = {'ETHUSDT': {'closed': 1, 'pnl_total': 10}}
        stats['by_direction'] = {'LONG': {'closed': 1}, 'SHORT': {'closed': 0}}
        stats['by_regime'] = {'bull': {'closed': 1}}
        stats['time'] = {'hour': {'14': {'closed': 1}}}
        insights = {
            'warnings': [],
            'summary': [
                {
                    'texto': 'Mayor pérdida histórica: ETHUSDT con +10.00 USDT.',
                    'datos_utilizados': {'symbol': 'ETHUSDT', 'pnl_usdt': 10},
                },
                {
                    'texto': 'ETHUSDT es el simbolo mas rentable.',
                    'datos_utilizados': {'symbol': 'ETHUSDT', 'closed': 1},
                },
            ],
        }

        with patch.object(telegram_commands, '_stats_payload', return_value=(stats, None)), \
             patch.object(telegram_commands, '_insights_payload', return_value=insights):
            text = telegram_commands._render_page('insights')['text']

        self.assertIn('Aún no hay suficientes operaciones para determinar la mayor pérdida.', text)
        self.assertIn('Muestra insuficiente para comparar símbolos.', text)
        self.assertNotIn('Mayor pérdida histórica', text)
        self.assertNotIn('ETHUSDT es el simbolo mas rentable', text)

    def test_capital_pending_rebalance_is_split_visually(self):
        metrics = {
            'total_real': 54.0,
            'total_limit': 54.0,
            'total_authorized': 54.0,
            'spot_real': 26.9,
            'spot_target': 0.0,
            'spot_used': 8.4,
            'spot_reserved': 0,
            'futures_real': 27.1,
            'futures_target': 54.0,
            'futures_used': 18.2,
            'futures_reserved': 0,
            'rebalance': {
                'status': 'PENDING',
                'direction': 'SPOT_TO_FUTURES',
                'amount_pending': 26.94,
                'last_check': '2026-07-13T12:00:00Z',
                'pending_reason': 'capital_outside_tolerance',
            },
            'max_exposure_percent': 80.0,
            'max_position_percent': None,
            'warning': None,
        }

        with patch.object(telegram_commands, '_exposure_metrics', return_value=metrics):
            text = telegram_commands._render_page('capital')['text']

        self.assertIn('Estado:', text)
        self.assertIn('Pendiente', text)
        self.assertIn('Desbalance: 26.94 USDT', text)
        self.assertEqual(text.count('Pendiente'), 1)

    def test_capital_hides_stale_pending_details_when_rebalance_is_reconciled(self):
        metrics = self._metrics()
        metrics['rebalance'] = {
            'status': 'NOT_REQUIRED',
            'direction': 'SPOT_TO_FUTURES',
            'amount_pending': 26.94,
            'reconciled': True,
            'resolved_reason': 'capital_already_aligned',
        }

        with patch.object(telegram_commands, '_exposure_metrics', return_value=metrics), \
             patch.object(telegram_commands, '_bot_state', return_value={}):
            text = telegram_commands._render_page('capital')['text']

        self.assertIn('Rebalance reconciliado', text)
        self.assertIn('Capital alineado dentro de la tolerancia', text)
        self.assertNotIn('Rebalance pendiente', text)
        self.assertNotIn('Desbalance pendiente:\n26.94 USDT', text)

    def test_capital_shows_targets_when_aligned_targets_match_real_capital(self):
        metrics = self._metrics()
        metrics.update({
            'spot_real': 26.90,
            'spot_target': 26.90,
            'futures_real': 27.10,
            'futures_target': 27.10,
            'rebalance': {'status': 'NOT_REQUIRED', 'tolerance': 0.20},
        })

        with patch.object(telegram_commands, '_exposure_metrics', return_value=metrics), \
             patch.object(telegram_commands, '_bot_state', return_value={}):
            text = telegram_commands._render_page('capital')['text']

        self.assertIn('Estado: ✅ Alineado', text)
        self.assertIn('Spot objetivo: 26.90 USDT', text)
        self.assertIn('Futures objetivo: 27.10 USDT', text)
        self.assertNotIn('Objetivo: No disponible', text)

    def test_capital_hides_targets_when_aligned_targets_do_not_match_real_capital(self):
        metrics = self._metrics()
        metrics.update({
            'spot_real': 0.10,
            'spot_target': 25.16,
            'futures_real': 49.84,
            'futures_target': 25.16,
            'rebalance': {'status': 'NOT_REQUIRED', 'tolerance': 0.20},
        })

        with patch.object(telegram_commands, '_exposure_metrics', return_value=metrics), \
             patch.object(telegram_commands, '_bot_state', return_value={}):
            text = telegram_commands._render_page('capital')['text']

        self.assertIn('Estado: ✅ Alineado', text)
        self.assertIn('Objetivo: No disponible', text)
        self.assertIn('objetivo reportado no coincide', text)
        self.assertNotIn('Spot objetivo: 25.16 USDT', text)
        self.assertNotIn('Futures objetivo: 25.16 USDT', text)

    def test_capital_incomplete_pending_rebalance_does_not_show_long_unknown_block(self):
        metrics = self._metrics()
        metrics['rebalance'] = {
            'status': 'PENDING',
            'direction': 'FUTURES_TO_SPOT',
            'amount_pending': 24.68,
        }

        with patch.object(telegram_commands, '_exposure_metrics', return_value=metrics), \
             patch.object(telegram_commands, '_bot_state', return_value={}):
            text = telegram_commands._render_page('capital')['text']

        self.assertIn('Datos de rebalance incompletos', text)
        self.assertIn('Detalle:', text)
        self.assertNotIn('Motivo desconocido', text)
        self.assertNotIn('Transferible:', text)
        self.assertNotIn('Capital Futures comprometido:', text)

    def test_capital_reuses_futures_values_for_pending_rebalance(self):
        metrics = self._metrics(short_count=2)
        metrics.update({
            'futures_available_balance': 35.26,
            'futures_position_margin': 14.60,
            'rebalance': {
                'status': 'PENDING',
                'direction': 'FUTURES_TO_SPOT',
                'amount_pending': 24.68,
                'last_check': '2026-07-13T12:00:00Z',
                'pending_reason': 'capital_outside_tolerance',
            },
            'futures_reconciliation': {
                'observed_count': 2,
                'managed_count': 2,
                'unmanaged_count': 0,
                'orphan_count': 0,
                'unprotected_count': 0,
                'desynced_count': 0,
                'allowed_count': 2,
                'aligned': True,
                'status': 'ALINEADO',
            },
        })

        with patch.object(telegram_commands, '_exposure_metrics', return_value=metrics), \
             patch.object(telegram_commands, '_bot_state', return_value={}):
            text = telegram_commands._render_page('capital')['text']

        self.assertIn('Shorts: 2/2 | Reconciliacion: ALINEADA', text)
        self.assertIn('Estado:', text)
        self.assertIn('Pendiente', text)
        self.assertIn('Spot objetivo: 26.90 USDT', text)
        self.assertIn('Futures objetivo: 27.10 USDT', text)
        self.assertIn('Transferible: 35.26 USDT', text)
        self.assertIn('Capital Futures comprometido: 14.60 USDT', text)
        self.assertNotIn('Shorts: 2/2 | Estado: ALINEADO', text)

    def test_system_shows_active_safety_pause(self):
        snapshot = {
            'safety_pause': {
                'active': True,
                'reason': 'daily_stop_loss_limit',
                'until': '2026-07-13T02:30:00Z',
            }
        }
        with patch.object(telegram_commands, '_bot_state', return_value=snapshot), \
             patch.object(telegram_commands, '_bot_status', return_value='PAUSED'), \
             patch.object(telegram_commands, '_guardian_status', return_value='ONLINE'), \
             patch.object(telegram_commands, '_dashboard_status', return_value='ONLINE'), \
             patch.object(telegram_commands, '_telegram_service_status', return_value='ONLINE'), \
             patch.object(telegram_commands, '_systemd_active_since', return_value='N/A'), \
             patch.object(telegram_commands, '_git_commit', return_value='abc123'), \
             patch.object(telegram_commands, '_git_deploy_time', return_value='N/A'), \
             patch.object(telegram_commands, '_server_uptime', return_value='N/A'):
            text = telegram_commands._render_page('system')['text']

        self.assertIn('Pausa de seguridad activa', text)
        self.assertIn('Motivo: 4 SL diarios', text)
        self.assertIn('Hasta: 23:30 UY', text)

    def test_system_does_not_show_inactive_safety_pause(self):
        snapshot = {'safety_pause': {'active': False, 'reason': 'daily_stop_loss_limit'}}
        with patch.object(telegram_commands, '_bot_state', return_value=snapshot), \
             patch.object(telegram_commands, '_bot_status', return_value='ONLINE'), \
             patch.object(telegram_commands, '_guardian_status', return_value='ONLINE'), \
             patch.object(telegram_commands, '_dashboard_status', return_value='ONLINE'), \
             patch.object(telegram_commands, '_telegram_service_status', return_value='ONLINE'), \
             patch.object(telegram_commands, '_systemd_active_since', return_value='N/A'), \
             patch.object(telegram_commands, '_git_commit', return_value='abc123'), \
             patch.object(telegram_commands, '_git_deploy_time', return_value='N/A'), \
             patch.object(telegram_commands, '_server_uptime', return_value='N/A'):
            text = telegram_commands._render_page('system')['text']

        self.assertNotIn('Pausa de seguridad activa', text)

    def test_capital_explains_futures_rebalance_blocked_by_open_positions(self):
        metrics = self._metrics(short_count=5)
        metrics.update({
            'futures_available_balance': 0.0,
            'futures_position_margin': 20.42,
            'futures_reconciliation': {
                'observed_count': 5,
                'managed_count': 0,
                'unprotected_count': 5,
                'position_margin': 20.42,
                'allowed_count': 0,
                'status': 'EXCESO FUTURES',
            },
        })

        with patch.object(telegram_commands, '_exposure_metrics', return_value=metrics), \
             patch.object(telegram_commands, '_bot_state', return_value={}):
            text = telegram_commands._render_page('capital')['text']

        self.assertIn('Comprometido: 20.42 USDT', text)
        self.assertIn('Disponible: 0.00 USDT', text)
        self.assertIn('Shorts:', text)
        self.assertIn('- Observadas: 5', text)
        self.assertIn('Gestionadas: 0', text)
        self.assertIn('- Permitidas ahora: 0', text)
        self.assertIn('- Sin proteccion: 5', text)
        self.assertIn('- Estado: EXCESO FUTURES', text)
        self.assertIn('Rebalance bloqueado porque hay posiciones Futures abiertas.', text)

    def test_capital_compacts_healthy_futures_reconciliation(self):
        metrics = self._metrics(short_count=0)
        metrics.update({
            'max_shorts': 0,
            'futures_used': 0.0,
            'futures_real': 0.10,
            'futures_reconciliation': {
                'observed_count': 0,
                'managed_count': 0,
                'unmanaged_count': 0,
                'orphan_count': 0,
                'unprotected_count': 0,
                'desynced_count': 0,
                'allowed_count': 2,
                'aligned': True,
                'status': 'ALINEADO',
            },
        })

        with patch.object(telegram_commands, '_exposure_metrics', return_value=metrics), \
             patch.object(telegram_commands, '_bot_state', return_value={}):
            text = telegram_commands._render_page('capital')['text']

        self.assertIn('Shorts: 0/0 | Reconciliacion: ALINEADA', text)
        self.assertNotIn('Shorts: 0/2', text)
        self.assertNotIn('Shorts observadas:', text)
        self.assertNotIn('Permitidas ahora:', text)
        self.assertNotIn('Sin proteccion:', text)
        self.assertNotIn('Estado: ALINEADO', text)

    def test_capital_compacts_managed_futures_without_position_risk(self):
        metrics = self._metrics(short_count=2)
        metrics.update({
            'max_shorts': 2,
            'futures_used': 17.72,
            'futures_real': 23.72,
            'futures_reconciliation': {
                'observed_count': 2,
                'managed_count': 2,
                'unmanaged_count': 0,
                'orphan_count': 0,
                'unprotected_count': 0,
                'desynced_count': 0,
                'allowed_count': 2,
                'aligned': False,
                'status': 'NO ALINEADO',
            },
        })
        with patch.object(telegram_commands, '_exposure_metrics', return_value=metrics), \
             patch.object(telegram_commands, '_bot_state', return_value={}):
            text = telegram_commands._render_page('capital')['text']

        self.assertIn('Shorts: 2/2', text)
        self.assertNotIn('- Observadas:', text)
        self.assertNotIn('Estado: NO ALINEADO', text)

    def test_capital_falls_back_to_live_short_count_when_summary_is_empty(self):
        metrics = self._metrics(short_count=5)
        metrics.update({
            'futures_available_balance': 0.0,
            'futures_position_margin': 20.42,
            'futures_reconciliation': {
                'observed_count': 0,
                'managed_count': 0,
                'unprotected_count': 0,
                'status': 'NO ALINEADO',
            },
        })

        with patch.object(telegram_commands, '_exposure_metrics', return_value=metrics), \
             patch.object(telegram_commands, '_bot_state', return_value={}):
            text = telegram_commands._render_page('capital')['text']

        self.assertIn('- Observadas: 5', text)

    def test_positions_shows_managed_spot_longs(self):
        state = {
            'positions': [
                {
                    'symbol': 'WLDUSDT',
                    'direction': 'long',
                    'entry_price': 10,
                    'quantity': 2,
                    'tp': 11,
                    'sl': 9,
                    'entry_time': '2026-01-01T00:00:00Z',
                }
            ]
        }

        with patch.object(telegram_commands, '_state', return_value=state), \
             patch.object(telegram_commands, '_bot_state', return_value={}), \
             patch.object(telegram_commands, '_public_price', return_value=10.5):
            text = telegram_commands._render_page('positions')['text']

        self.assertIn('📈 Spot', text)
        self.assertIn('WLD L', text)
        self.assertIn('PnL abierto total: +1.00 USDT', text)
        self.assertIn('PnL: +1.00 USDT (+5.0%)', text)
        self.assertIn('TP: +4.8% (+1.00 USDT) | SL: -14.3% (-3.00 USDT)', text)

    def test_positions_shows_observed_futures_shorts(self):
        bot_snapshot = {
            'positions': {
                'short': {
                    'observed': [
                        {
                            'symbol': 'CRCLUSDT',
                            'side': 'SHORT',
                            'notional': 12.34,
                            'quantity': 102.8333333333,
                            'entry_price': 0.1234,
                            'mark_price': 0.1200,
                            'unrealized_pnl': 0.42,
                            'tp': 0.115,
                            'sl': 0.126,
                            'opened_at': '2026-01-01T00:00:00Z',
                        }
                    ]
                }
            }
        }

        with patch.object(telegram_commands, '_state', return_value={'positions': []}), \
             patch.object(telegram_commands, '_bot_state', return_value=bot_snapshot), \
             patch.object(telegram_commands, '_futures_reconciliation_entry', return_value={}):
            text = telegram_commands._render_page('positions')['text']

        self.assertIn('📉 Futures', text)
        self.assertIn('PnL abierto total: +0.42 USDT', text)
        self.assertIn('CRCL S | abierto', text)
        self.assertIn('PnL: +0.42 USDT (+2.8%)', text)
        self.assertIn('TP: +4.2% (+0.51 USDT) | SL: -5.0% (-0.62 USDT)', text)
        self.assertNotIn('Notional', text)
        self.assertNotIn('Lev x5', text)
        self.assertNotIn('Entry', text)
        self.assertNotIn('Mark', text)

    def test_positions_marks_unprotected_desynced_futures(self):
        bot_snapshot = {
            'positions': {
                'short': {
                    'observed': [{'symbol': 'CRCLUSDT', 'side': 'SHORT', 'notional': 11.67}]
                }
            }
        }
        entry = {
            'classification': [
                'observed_futures_position',
                'unmanaged_futures_position',
                'orphan_futures_position',
                'unprotected_futures_position',
                'desynced_closed_but_open_on_exchange',
            ]
        }

        with patch.object(telegram_commands, '_state', return_value={'positions': []}), \
             patch.object(telegram_commands, '_bot_state', return_value=bot_snapshot), \
             patch.object(telegram_commands, '_futures_reconciliation_entry', return_value=entry):
            text = telegram_commands._render_page('positions')['text']

        self.assertIn('Observada en Binance', text)
        self.assertIn('No gestionada / huerfana', text)
        self.assertIn('Sin proteccion', text)
        self.assertIn('Cerrada en historial, abierta en exchange', text)

    def test_positions_deduplicates_futures_by_symbol(self):
        state = {
            'positions': [
                {
                    'symbol': 'CRCLUSDT',
                    'direction': 'short',
                    'entry_price': 0.12,
                    'quantity': 100,
                    'tp': 0.10,
                    'sl': 0.13,
                    'leverage': 5,
                }
            ]
        }
        bot_snapshot = {
            'positions': {
                'short': {
                    'observed': [{'symbol': 'CRCLUSDT', 'side': 'SHORT', 'notional': 12.34}]
                }
            }
        }

        with patch.object(telegram_commands, '_state', return_value=state), \
             patch.object(telegram_commands, '_bot_state', return_value=bot_snapshot), \
             patch.object(telegram_commands, '_futures_reconciliation_entry', return_value={}), \
             patch.object(telegram_commands, '_public_price', return_value=0.11):
            text = telegram_commands._render_page('positions')['text']

        self.assertEqual(text.count('CRCL S'), 1)
        self.assertIn('TP: +9.1% (+1.00 USDT)', text)

    def test_positions_degrades_when_futures_fields_are_missing(self):
        bot_snapshot = {
            'positions': {
                'short': {
                    'observed': [{'symbol': 'SUIUSDT', 'side': 'SHORT'}]
                }
            }
        }

        with patch.object(telegram_commands, '_state', return_value={'positions': []}), \
             patch.object(telegram_commands, '_bot_state', return_value=bot_snapshot), \
             patch.object(telegram_commands, '_futures_reconciliation_entry', return_value={}):
            text = telegram_commands._render_page('positions')['text']

        self.assertIn('SUI S', text)
        self.assertIn('PnL abierto total: N/A', text)
        self.assertIn('No disponible', text)

    def test_positions_compact_format_groups_spot_and_futures(self):
        state = {
            'positions': [
                {'symbol': 'WLDUSDT', 'direction': 'long', 'entry_price': 10, 'quantity': 1, 'tp': 11, 'sl': 9}
            ]
        }
        bot_snapshot = {
            'positions': {
                'short': {
                    'observed': [{'symbol': 'BNBUSDT', 'side': 'SHORT', 'notional': 20, 'unrealized_pnl': -0.5}]
                }
            }
        }

        with patch.object(telegram_commands, '_state', return_value=state), \
             patch.object(telegram_commands, '_bot_state', return_value=bot_snapshot), \
             patch.object(telegram_commands, '_futures_reconciliation_entry', return_value={}), \
             patch.object(telegram_commands, '_public_price', return_value=10.5):
            text = telegram_commands._render_page('positions')['text']

        self.assertIn('📈 Spot', text)
        self.assertIn('📉 Futures', text)
        self.assertNotIn('\nPnL:\n', text)
        self.assertNotIn('🎯 TP', text)
        self.assertNotIn('Notional', text)
        self.assertLessEqual(len([line for line in text.splitlines() if line.strip()]), 10)


if __name__ == '__main__':
    unittest.main()
