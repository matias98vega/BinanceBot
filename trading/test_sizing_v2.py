#!/usr/bin/env python3
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import config
import utils


class SizingV2Tests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            'BOT_TOTAL_CAPITAL_LIMIT_USDT': '1000',
            'BOT_MAX_EXPOSURE_PERCENT': '80',
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_spot_two_slots_target_eighty_percent_total(self):
        capital = utils.get_spot_capital_per_position({'positions': []}, spot_free=50.0, max_longs=2)

        self.assertAlmostEqual(20.0, capital, places=6)

    def test_spot_three_slots_target_eighty_percent_total(self):
        capital = utils.get_spot_capital_per_position({'positions': []}, spot_free=50.0, max_longs=3)

        self.assertAlmostEqual(40.0 / 3.0, capital, places=6)

    def test_spot_existing_position_uses_remaining_exposure_without_overcompensation(self):
        state = {'positions': [{'direction': 'long', 'entry_price': 10.0, 'quantity': 2.0}]}

        capital = utils.get_spot_capital_per_position(state, spot_free=30.0, max_longs=2)

        self.assertAlmostEqual(20.0, capital, places=6)

    def test_spot_free_balance_caps_slot_without_exceeding_available_cash(self):
        with patch.dict(os.environ, {'BOT_MAX_EXPOSURE_PERCENT': '100'}):
            capital = utils.get_spot_capital_per_position({'positions': []}, spot_free=8.0, max_longs=1)

        self.assertAlmostEqual(7.6, capital, places=6)

    @patch('utils.get_futures_summary', return_value=(50.0, 50.0, 0.0))
    def test_futures_two_slots_target_notional_not_margin(self, _summary):
        notional = utils.get_futures_notional_per_position({'positions': []}, max_shorts=2)

        self.assertAlmostEqual(20.0, notional, places=6)
        self.assertAlmostEqual(10.0, notional / config.FUTURES_LEVERAGE, places=6)

    @patch('utils.get_futures_summary', return_value=(50.0, 50.0, 0.0))
    def test_futures_three_slots_target_notional(self, _summary):
        notional = utils.get_futures_notional_per_position({'positions': []}, max_shorts=3)

        self.assertAlmostEqual(40.0 / 3.0, notional, places=6)
        self.assertAlmostEqual((40.0 / 3.0) / config.FUTURES_LEVERAGE, notional / config.FUTURES_LEVERAGE, places=6)

    @patch('utils.get_futures_summary', return_value=(50.0, 50.0, 0.0))
    def test_futures_leverage_does_not_multiply_notional_limit(self, _summary):
        first = utils.get_futures_notional_per_position({'positions': []}, max_shorts=2)
        state = {'positions': [{'direction': 'short', 'entry_price': 10.0, 'quantity': 2.0}]}
        second = utils.get_futures_notional_per_position(state, max_shorts=2)

        self.assertAlmostEqual(20.0, first, places=6)
        self.assertAlmostEqual(20.0, second, places=6)
        self.assertLessEqual(first + second, 40.0)

    @patch('utils.get_futures_summary', return_value=(50.0, 4.0, 0.0))
    def test_futures_available_margin_caps_notional_safely(self, _summary):
        notional = utils.get_futures_notional_per_position({'positions': []}, max_shorts=2)

        self.assertAlmostEqual(4.0 * 0.95 * config.FUTURES_LEVERAGE, notional, places=6)

    @patch('utils.get_futures_summary', return_value=(50.0, 50.0, 0.0))
    def test_futures_existing_short_respects_remaining_notional(self, _summary):
        state = {'positions': [{'direction': 'short', 'entry_price': 10.0, 'quantity': 3.0}]}

        notional = utils.get_futures_notional_per_position(state, max_shorts=2)

        self.assertAlmostEqual(10.0, notional, places=6)


if __name__ == '__main__':
    unittest.main()
