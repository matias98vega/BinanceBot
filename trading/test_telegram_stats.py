#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import unittest
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
            'BULL': {'trades': 1, 'win_rate': 100, 'pnl_total': 10},
            'BEAR': {'trades': 1, 'win_rate': 0, 'pnl_total': -5},
            'SIDEWAYS': {'trades': 0, 'win_rate': 0, 'pnl_total': 0},
            'NEUTRAL': {'trades': 0, 'win_rate': 0, 'pnl_total': 0},
            'UNKNOWN': {'trades': 1, 'win_rate': 0, 'pnl_total': 0},
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

    def tearDown(self):
        self.tmp.cleanup()

    def _patch_stats(self, stats=None):
        return patch.multiple(
            telegram_commands.analytics_engine,
            DEFAULT_STATS_FILE=self.stats_file,
            load_stats=lambda: stats or sample_stats(),
        )

    def test_stats_general_format(self):
        with self._patch_stats():
            response = telegram_commands._render_page('stats_general')

        text = response['text']
        self.assertIn('Resumen General', text)
        self.assertIn('Trades totales: 3', text)
        self.assertIn('Win Rate: 50.0%', text)
        self.assertIn('Profit Factor: 2.00', text)
        self.assertIn('PnL total: +5.00 USDT', text)

    def test_stats_symbols_format(self):
        with self._patch_stats():
            response = telegram_commands._render_page('stats_symbols')

        text = response['text']
        self.assertIn('Por simbolo', text)
        self.assertLess(text.find('ETHUSDT'), text.find('BTCUSDT'))
        self.assertIn('PF N/A', text)

    def test_stats_missing_file_warns_and_uses_engine(self):
        with self._patch_stats():
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
            'pnl': {'today': 99, 'total': 99},
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

        self.assertIn('PnL hoy: +1.23 USDT', text)
        self.assertIn('PnL total: +12.34 USDT', text)
        self.assertIn('Spot: 8.40 USDT / 26.90 USDT', text)
        self.assertIn('Futures: 18.20 USDT / 27.10 USDT', text)
        self.assertNotIn('+99.00 USDT', text)

    def test_insights_low_sample_suppresses_misleading_comparisons(self):
        stats = sample_stats()
        stats['general']['closed_trades'] = 1
        stats['general']['closed'] = 1
        stats['by_symbol'] = {'ETHUSDT': {'closed': 1, 'pnl_total': 10}}
        stats['by_direction'] = {'LONG': {'closed': 1}, 'SHORT': {'closed': 0}}
        stats['by_regime'] = {'BULL': {'closed': 1}}
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
            },
            'max_exposure_percent': 80.0,
            'max_position_percent': None,
            'warning': None,
        }

        with patch.object(telegram_commands, '_exposure_metrics', return_value=metrics):
            text = telegram_commands._render_page('capital')['text']

        self.assertIn('Rebalance pendiente', text)
        self.assertIn('Dirección:\nSpot → Futures', text)
        self.assertIn('Monto:\n26.94 USDT', text)


if __name__ == '__main__':
    unittest.main()
