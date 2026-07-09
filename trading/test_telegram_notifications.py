#!/usr/bin/env python3
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import bot_state
import notification_guard
import telegram_alerts
import telegram_commands
import utils


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

    @patch.dict(os.environ, {
        'BINANCEBOT_TEST_MODE': 'true',
        'BINANCEBOT_DISABLE_EXTERNAL_NOTIFICATIONS': 'true',
        'TELEGRAM_ALERTS_ENABLED': 'true',
        'TELEGRAM_BOT_TOKEN': 'real-token-from-env',
        'TELEGRAM_CHAT_ID': 'real-chat',
    }, clear=False)
    def test_test_mode_suppresses_telegram_transport(self):
        with patch.object(telegram_alerts, '_send_raw') as send_raw, \
             self.assertLogs(level='INFO') as logs:
            sent = telegram_alerts.send_telegram_alert('WARNING', 'BinanceBot', 'NEAR residual sin OCO')

        self.assertFalse(sent)
        send_raw.assert_not_called()
        self.assertIn('external notification suppressed in test mode', '\n'.join(logs.output))

    @patch.dict(os.environ, {
        'BINANCEBOT_TEST_MODE': '',
        'BINANCEBOT_DISABLE_EXTERNAL_NOTIFICATIONS': '',
        'TELEGRAM_ALERTS_ENABLED': 'true',
        'TELEGRAM_BOT_TOKEN': 'real-token-from-env',
        'TELEGRAM_CHAT_ID': 'real-chat',
    }, clear=False)
    def test_unittest_argv_suppresses_telegram_transport(self):
        with patch.object(notification_guard, 'argv_indicates_test', return_value=True), \
             patch.object(telegram_alerts, '_send_raw') as send_raw:
            sent = telegram_alerts.send_telegram_alert('WARNING', 'BinanceBot', 'NEAR residual sin OCO')

        self.assertFalse(sent)
        send_raw.assert_not_called()

    @patch.dict(os.environ, {
        'BINANCEBOT_TEST_MODE': '',
        'BINANCEBOT_DISABLE_EXTERNAL_NOTIFICATIONS': '',
        'TELEGRAM_ALERTS_ENABLED': 'true',
        'TELEGRAM_BOT_TOKEN': 'mock-token',
        'TELEGRAM_CHAT_ID': 'mock-chat',
        'TELEGRAM_ALERT_COOLDOWN_SECONDS': '1',
    }, clear=False)
    def test_production_mode_uses_mocked_transport(self):
        with patch.object(notification_guard, 'argv_indicates_test', return_value=False), \
             patch.object(telegram_alerts, '_cooldown_suppressed', return_value=(False, 'fp')), \
             patch.object(telegram_alerts, '_record_sent') as record_sent, \
             patch.object(telegram_alerts, '_send_raw', return_value=True) as send_raw:
            sent = telegram_alerts.send_telegram_alert('WARNING', 'BinanceBot', 'production alert')

        self.assertTrue(sent)
        send_raw.assert_called_once()
        record_sent.assert_called_once()

    @patch.dict(os.environ, {
        'BINANCEBOT_TEST_MODE': 'true',
        'BINANCEBOT_DISABLE_EXTERNAL_NOTIFICATIONS': 'true',
        'TELEGRAM_ALERTS_ENABLED': 'true',
        'TELEGRAM_BOT_TOKEN': 'real-token-from-env',
        'TELEGRAM_CHAT_ID': 'real-chat',
    }, clear=False)
    def test_utils_send_alert_suppresses_all_external_transports_in_test_mode(self):
        with patch('telegram_alerts.send_telegram_alert') as telegram_alert, \
             patch('subprocess.run') as subprocess_run:
            utils.send_alert('NEAR residual sin OCO')

        telegram_alert.assert_not_called()
        subprocess_run.assert_not_called()

    @patch.dict(os.environ, {
        'BINANCEBOT_TEST_MODE': 'true',
        'BINANCEBOT_DISABLE_EXTERNAL_NOTIFICATIONS': 'true',
    }, clear=False)
    def test_telegram_commands_transport_is_suppressed_in_test_mode(self):
        with patch('urllib.request.urlopen') as urlopen:
            response = telegram_commands._telegram_request(
                'real-token-from-env',
                'sendMessage',
                {'chat_id': 'real-chat', 'text': 'test'},
            )

        self.assertEqual({'ok': False, 'suppressed': True}, response)
        urlopen.assert_not_called()


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
