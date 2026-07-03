#!/usr/bin/env python3
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import bot_state
import telegram_alerts


class TelegramNotificationConfigTests(unittest.TestCase):
    @patch.dict(os.environ, {'TELEGRAM_NOTIFY_OPEN': 'false'}, clear=False)
    def test_notification_type_can_be_disabled(self):
        self.assertFalse(telegram_alerts.notification_enabled('OPEN', 'INFO'))

    @patch.dict(os.environ, {'TELEGRAM_NOTIFY_OPEN': 'true'}, clear=False)
    def test_notification_type_can_be_enabled(self):
        self.assertTrue(telegram_alerts.notification_enabled('OPEN', 'INFO'))

    @patch.dict(os.environ, {'TELEGRAM_NOTIFY_BLACKLIST': ''}, clear=False)
    def test_blacklist_default_disabled(self):
        self.assertFalse(telegram_alerts.notification_enabled('BLACKLIST', 'WARNING'))


class ObservableCapacityTests(unittest.TestCase):
    def test_dynamic_capacity_is_not_capped_by_static_config(self):
        self.assertEqual(bot_state._wallet_max_positions(100, configured_max=2, dynamic_value=4), 4)

    def test_zero_target_still_disables_capacity(self):
        self.assertEqual(bot_state._wallet_max_positions(0, configured_max=2, dynamic_value=4), 0)

    def test_futures_account_observability_counts_open_shorts_and_margin(self):
        account = {
            'totalWalletBalance': '22.16',
            'availableBalance': '0.00',
            'totalPositionInitialMargin': '20.42',
            'positions': [
                {'symbol': 'CRCLUSDT', 'positionAmt': '-1'},
                {'symbol': 'SUIUSDT', 'positionAmt': '-2'},
                {'symbol': 'NEARUSDT', 'positionAmt': '-3'},
                {'symbol': 'HYPEUSDT', 'positionAmt': '-4'},
                {'symbol': 'BNBUSDT', 'positionAmt': '-5'},
                {'symbol': 'BTCUSDT', 'positionAmt': '0'},
            ],
        }

        observability = bot_state.futures_observability_from_account(account)

        self.assertEqual(observability['futures_open_positions_count'], 5)
        self.assertEqual(observability['futures_position_margin'], 20.42)
        self.assertEqual(observability['futures_available_balance'], 0.0)
        self.assertEqual(len(observability['futures_positions']), 5)
        self.assertEqual(observability['futures_positions'][0]['symbol'], 'CRCLUSDT')
        self.assertEqual(observability['futures_positions'][0]['side'], 'SHORT')

    @patch.dict(os.environ, {'BOT_TOTAL_CAPITAL_LIMIT_USDT': '54'}, clear=False)
    def test_bot_state_uses_observed_futures_positions_for_read_model(self):
        payload = bot_state.build_bot_state(
            state={'positions': []},
            btc_ctx={'trend': 'bullish', 'btc_price': 60000, 'change_4h': 1.0},
            spot_real=31.85,
            futures_real=22.16,
            futures_observability={
                'futures_open_positions_count': 5,
                'futures_position_margin': 20.42,
                'futures_available_balance': 0.0,
                'futures_wallet_balance': 22.16,
                'futures_positions': [{'symbol': 'CRCLUSDT', 'side': 'SHORT'}],
            },
            max_longs=2,
            max_shorts=0,
        )

        self.assertEqual(payload['positions']['short']['current'], 5)
        self.assertEqual(payload['capital']['futures_used'], 20.42)
        self.assertEqual(payload['capital']['futures_available_balance'], 0.0)
        self.assertEqual(payload['positions']['short']['observed'], [{'symbol': 'CRCLUSDT', 'side': 'SHORT'}])


if __name__ == '__main__':
    unittest.main()
