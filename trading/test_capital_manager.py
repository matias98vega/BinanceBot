#!/usr/bin/env python3
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import capital_manager


BASE_ENV = {
    'BOT_TOTAL_CAPITAL_LIMIT_USDT': '1000000',
    'BOT_SPOT_CAPITAL_LIMIT_USDT': '',
    'BOT_FUTURES_CAPITAL_LIMIT_USDT': '',
    'BOT_MAX_EXPOSURE_PERCENT': '80',
    'BOT_MAX_POSITION_PERCENT': '',
}


class CapitalSizingTests(unittest.TestCase):
    def test_margin_per_position_100_capital_two_slots(self):
        self.assertEqual(capital_manager.max_margin_per_position(100, 2, 80), 40)

    def test_margin_per_position_50_capital_two_slots(self):
        self.assertEqual(capital_manager.max_margin_per_position(50, 2, 80), 20)

    def test_margin_per_position_single_slot(self):
        self.assertEqual(capital_manager.max_margin_per_position(50, 1, 80), 40)

    def test_margin_per_position_never_negative(self):
        self.assertEqual(capital_manager.max_margin_per_position(-1, 2, 80), 0)

    @patch.dict(os.environ, BASE_ENV, clear=False)
    def test_validate_accepts_exact_futures_max(self):
        ok, msg, payload = capital_manager.validate_futures_order(
            {'positions': []}, 100, 40, max_positions=2
        )
        self.assertTrue(ok, msg)
        self.assertEqual(payload['max_margin_per_position'], 40)

    @patch.dict(os.environ, BASE_ENV, clear=False)
    def test_validate_rejects_above_futures_max(self):
        ok, msg, payload = capital_manager.validate_futures_order(
            {'positions': []}, 100, 40.0000001, max_positions=2
        )
        self.assertFalse(ok, msg)
        self.assertEqual(payload['max_margin_per_position'], 40)

    @patch.dict(os.environ, BASE_ENV, clear=False)
    def test_validate_accepts_exact_spot_max(self):
        ok, msg, payload = capital_manager.validate_spot_order(
            {'positions': []}, 100, 40, max_positions=2
        )
        self.assertTrue(ok, msg)
        self.assertEqual(payload['max_margin_per_position'], 40)

    @patch.dict(os.environ, BASE_ENV, clear=False)
    def test_validate_rejects_above_spot_max(self):
        ok, msg, payload = capital_manager.validate_spot_order(
            {'positions': []}, 100, 40.0000001, max_positions=2
        )
        self.assertFalse(ok, msg)
        self.assertEqual(payload['max_margin_per_position'], 40)


if __name__ == '__main__':
    unittest.main()
