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


if __name__ == '__main__':
    unittest.main()
