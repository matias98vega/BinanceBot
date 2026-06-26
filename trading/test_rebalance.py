#!/usr/bin/env python3
import os
import sys
import unittest

os.environ.setdefault('BINANCE_API_KEY', 'test')
os.environ.setdefault('BINANCE_API_SECRET', 'test')

sys.path.insert(0, os.path.dirname(__file__))

import rebalance


class RebalanceReserveTests(unittest.TestCase):
    def test_transfer_amount_with_zero_wallet_reserve(self):
        amount = rebalance._transferable_amount(
            required_amount=51.41,
            source_free=51.41,
            wallet_min=0,
        )
        self.assertEqual(amount, 51.41)

    def test_transfer_amount_with_configured_wallet_reserve(self):
        amount = rebalance._transferable_amount(
            required_amount=51.41,
            source_free=51.41,
            wallet_min=3,
        )
        self.assertEqual(amount, 48.41)


if __name__ == '__main__':
    unittest.main()
