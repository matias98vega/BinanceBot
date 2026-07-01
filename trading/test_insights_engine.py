#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))

import insights_engine


def bucket(trades=0, closed=0, win=0, loss=0, pnl=0, win_rate=0, pf=None, expectancy=0):
    return {
        'trades': trades,
        'closed': closed,
        'open': max(trades - closed, 0),
        'win': win,
        'loss': loss,
        'breakeven': max(closed - win - loss, 0),
        'win_rate': win_rate,
        'profit_factor': pf,
        'expectancy': expectancy,
        'pnl_total': pnl,
        'pnl_average': expectancy,
        'gross_profit': max(pnl, 0),
        'gross_loss': abs(min(pnl, 0)),
        'duration_average_minutes': 30,
    }


class InsightsEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stats_file = os.path.join(self.tmp.name, 'stats.json')
        self.insights_file = os.path.join(self.tmp.name, 'insights.json')
        self.stats = {
            'general': {
                **bucket(trades=14, closed=14, win=8, loss=6, pnl=4.5, win_rate=57.1, pf=1.4, expectancy=0.32),
                'closed_trades': 14,
                'open_trades': 0,
                'total_trades': 14,
                'best_trade': {'symbol': 'ADAUSDT', 'pnl_usdt': 3.2},
                'worst_trade': {'symbol': 'XRPUSDT', 'pnl_usdt': -2.1},
                'max_drawdown_usdt': 2.8,
            },
            'by_symbol': {
                'ADAUSDT': bucket(trades=7, closed=7, win=5, loss=2, pnl=6.0, win_rate=71.4, pf=2.2, expectancy=0.86),
                'XRPUSDT': bucket(trades=6, closed=6, win=2, loss=4, pnl=-3.0, win_rate=33.3, pf=0.5, expectancy=-0.5),
                'ETHUSDT': bucket(trades=1, closed=1, win=1, loss=0, pnl=1.5, win_rate=100, pf=None, expectancy=1.5),
            },
            'by_direction': {
                'LONG': bucket(trades=6, closed=6, win=2, loss=4, pnl=-2.0, win_rate=33.3, pf=0.6, expectancy=-0.33),
                'SHORT': bucket(trades=8, closed=8, win=6, loss=2, pnl=6.5, win_rate=75.0, pf=2.4, expectancy=0.81),
            },
            'by_regime': {
                'bull': bucket(trades=5, closed=5, win=3, loss=2, pnl=2.0, win_rate=60, pf=1.5, expectancy=0.4),
                'bear': bucket(trades=6, closed=6, win=4, loss=2, pnl=4.0, win_rate=66.7, pf=2.0, expectancy=0.67),
                'sideways': bucket(trades=3, closed=3, win=1, loss=2, pnl=-1.5, win_rate=33.3, pf=0.5, expectancy=-0.5),
            },
            'by_exit_reason': {
                'TP': bucket(trades=5, closed=5),
                'SL': bucket(trades=4, closed=4),
                'TRAILING': bucket(trades=2, closed=2),
                'PARTIAL': bucket(trades=1, closed=1),
                'RECOVERY': bucket(trades=1, closed=1),
                'MANUAL': bucket(trades=1, closed=1),
                'EMERGENCY': bucket(trades=0, closed=0),
            },
            'time': {
                'hour': {
                    '14': bucket(trades=6, closed=6, win=4, loss=2, pnl=5.0, win_rate=66.7, pf=2.0, expectancy=0.83),
                    '03': bucket(trades=5, closed=5, win=1, loss=4, pnl=-4.0, win_rate=20, pf=0.4, expectancy=-0.8),
                },
                'day': {
                    '2026-06-28': bucket(trades=6, closed=6, win=5, loss=1, pnl=5.0, win_rate=83.3, pf=3.0, expectancy=0.83),
                    '2026-06-29': bucket(trades=6, closed=6, win=2, loss=4, pnl=-1.0, win_rate=33.3, pf=1.0, expectancy=-0.17),
                },
                'week': {
                    '2026-W26': bucket(trades=6, closed=6, win=5, loss=1, pnl=5.0, win_rate=83.3, pf=3.0, expectancy=0.83),
                    '2026-W27': bucket(trades=6, closed=6, win=2, loss=4, pnl=-1.0, win_rate=33.3, pf=1.0, expectancy=-0.17),
                },
                'month': {},
            },
        }
        with open(self.stats_file, 'w', encoding='utf-8') as f:
            json.dump(self.stats, f)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rebuild_writes_insights(self):
        result = insights_engine.rebuild_insights(self.stats_file, self.insights_file)

        self.assertTrue(os.path.exists(self.insights_file))
        self.assertIn('GENERAL', result['insights'])
        self.assertTrue(result['summary'])

    def test_missing_file_rebuilds(self):
        result = insights_engine.load_insights(self.insights_file, stats_file=self.stats_file)

        self.assertTrue(os.path.exists(self.insights_file))
        self.assertIn('SIMBOLOS', result['insights'])

    def test_corrupt_file_rebuilds_with_warning(self):
        with open(self.insights_file, 'w', encoding='utf-8') as f:
            f.write('{bad json')

        result = insights_engine.load_insights(self.insights_file, stats_file=self.stats_file)

        self.assertTrue(result['warnings'])
        self.assertTrue(result['summary'])

    def test_comparison_alerts(self):
        result = insights_engine.rebuild_insights(self.stats_file, self.insights_file)

        texts = [item['texto'] for item in result['alerts']]
        self.assertTrue(any('Win Rate diario cayo' in text for text in texts))
        self.assertTrue(any('Profit Factor semanal cayo' in text for text in texts))

    def test_symbol_insights(self):
        result = insights_engine.rebuild_insights(self.stats_file, self.insights_file)
        texts = [item['texto'] for item in result['insights']['SIMBOLOS']]

        self.assertTrue(any('ADAUSDT es el simbolo mas rentable' in text for text in texts))
        self.assertTrue(any('XRPUSDT esta en PnL negativo' in text for text in texts))

    def test_regime_insights(self):
        result = insights_engine.rebuild_insights(self.stats_file, self.insights_file)
        texts = [item['texto'] for item in result['insights']['REGIMEN']]

        self.assertTrue(any('Bear es el regimen mas rentable' in text for text in texts))

    def test_long_vs_short_insights(self):
        result = insights_engine.rebuild_insights(self.stats_file, self.insights_file)
        texts = [item['texto'] for item in result['insights']['LONG_VS_SHORT']]

        self.assertTrue(any('SHORT rinde mejor' in text for text in texts))

    def test_accessors(self):
        insights_engine.rebuild_insights(self.stats_file, self.insights_file)

        self.assertTrue(insights_engine.get_general_insights(self.insights_file))
        self.assertTrue(insights_engine.get_symbol_insights(self.insights_file))
        self.assertTrue(insights_engine.get_risk_insights(self.insights_file))
        self.assertTrue(insights_engine.get_temporal_insights(self.insights_file))
        self.assertIn('insights', insights_engine.get_all_insights(self.insights_file))


if __name__ == '__main__':
    unittest.main()
