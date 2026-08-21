#!/usr/bin/env python3
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import bot_state
import market
import notification_guard
import telegram_alerts
import telegram_commands
import utils
from orchestration import cycle_runner


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


class TelegramPreventiveEpisodeTests(unittest.TestCase):
    SHORT_EVENT = cycle_runner.PREVENTIVE_BTC_RISE_CLOSE_SHORTS_EVENT
    LONG_EVENT = cycle_runner.PREVENTIVE_BTC_FALL_CLOSE_LONGS_EVENT

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp')
        self.state_path = os.path.join(self.temp.name, 'telegram_alert_state.json')
        self.env = patch.dict(os.environ, {
            'BINANCEBOT_TEST_MODE': '',
            'BINANCEBOT_DISABLE_EXTERNAL_NOTIFICATIONS': '',
            'TELEGRAM_ALERTS_ENABLED': 'true',
            'TELEGRAM_BOT_TOKEN': 'offline-fixture-token',
            'TELEGRAM_CHAT_ID': 'offline-fixture-chat',
            'TELEGRAM_ALERT_LEVEL': 'WARNING',
            'TELEGRAM_ALERT_COOLDOWN_SECONDS': '1800',
        }, clear=False)
        self.env.start()
        self.state_file = patch.object(telegram_alerts, 'ALERT_STATE_FILE', self.state_path)
        self.state_file.start()
        self.notifications = patch.object(
            telegram_alerts,
            'external_notifications_disabled',
            return_value=False,
        )
        self.notifications.start()
        self.transport = patch.object(telegram_alerts, '_send_raw', return_value=True)
        self.send_raw = self.transport.start()

    def tearDown(self):
        self.transport.stop()
        self.notifications.stop()
        self.state_file.stop()
        self.env.stop()
        self.temp.cleanup()

    @staticmethod
    def _message(change):
        close_shorts, close_longs, message = market.check_btc_momentum_close({
            'change_4h': change,
        })
        return close_shorts, close_longs, message

    def _send(self, change, event_key=None):
        close_shorts, close_longs, message = self._message(change)
        if event_key is None:
            event_key = self.SHORT_EVENT if close_shorts else self.LONG_EVENT
        return telegram_alerts.send_telegram_alert(
            'WARNING',
            'BinanceBot',
            message,
            notification_type='WARNING',
            event_key=event_key,
        )

    def _state(self):
        with open(self.state_path, encoding='utf-8') as stream:
            return json.load(stream)

    def test_t1_first_activation_sends(self):
        self.assertTrue(self._send(4.10))
        self.assertEqual(1, self.send_raw.call_count)
        self.assertTrue(self._state()['event_conditions'][self.SHORT_EVENT]['active'])

    def test_t2_same_event_with_different_percentage_is_suppressed(self):
        self.assertTrue(self._send(4.10))
        self.assertFalse(self._send(4.18))
        self.assertEqual(1, self.send_raw.call_count)

    def test_t3_ten_active_cycles_send_exactly_once(self):
        results = [self._send(value) for value in (4.10, 4.18, 4.07, 4.24, 4.31,
                                                    4.12, 4.46, 4.09, 4.27, 4.15)]
        self.assertEqual(1, sum(result is True for result in results))
        self.assertEqual(1, self.send_raw.call_count)

    def test_t4_inactive_transition_rearms(self):
        self.assertTrue(self._send(4.10))
        self.assertTrue(telegram_alerts.rearm_alert_event(self.SHORT_EVENT))
        event = self._state()['event_conditions'][self.SHORT_EVENT]
        self.assertFalse(event['active'])
        self.assertIn('rearmed_at', event)

    def test_t5_reactivation_sends_second_episode(self):
        self.assertTrue(self._send(4.10))
        self.assertTrue(telegram_alerts.rearm_alert_event(self.SHORT_EVENT))
        self.assertTrue(self._send(4.18))
        self.assertEqual(2, self.send_raw.call_count)

    def test_t6_persisted_active_state_survives_simulated_restart(self):
        self.assertTrue(self._send(4.10))
        persisted = self._state()['event_conditions'][self.SHORT_EVENT]
        self.assertTrue(persisted['active'])
        with patch.object(telegram_alerts, '_read_state', wraps=telegram_alerts._read_state) as read_state:
            self.assertFalse(self._send(4.18))
        read_state.assert_called()
        self.assertEqual(1, self.send_raw.call_count)

    def test_t7_different_direction_uses_independent_event_key(self):
        self.assertTrue(self._send(4.10, self.SHORT_EVENT))
        self.assertTrue(self._send(-4.10, self.LONG_EVENT))
        self.assertEqual(2, self.send_raw.call_count)
        with patch.object(cycle_runner.utils, 'send_alert') as send_alert, \
             patch.object(cycle_runner.utils, 'rearm_telegram_alert_event') as rearm:
            cycle_runner.sync_preventive_telegram_alert(True, False, 'fixture')
        send_alert.assert_called_once_with('fixture', event_key=self.SHORT_EVENT)
        rearm.assert_called_once_with(self.LONG_EVENT)

    def test_t8_display_message_changes_but_event_identity_is_stable(self):
        first = self._message(4.10)[2]
        second = self._message(4.18)[2]
        self.assertNotEqual(first, second)
        self.assertIn('+4.1%', first)
        self.assertIn('+4.2%', second)
        first_fp = telegram_alerts._fingerprint(
            'WARNING', 'BinanceBot', first, event_key=self.SHORT_EVENT)
        second_fp = telegram_alerts._fingerprint(
            'WARNING', 'BinanceBot', second, event_key=self.SHORT_EVENT)
        self.assertEqual(first_fp, second_fp)

    def test_t9_corrupt_state_warns_sends_and_rebuilds_safe_state(self):
        with open(self.state_path, 'w', encoding='utf-8') as stream:
            stream.write('{invalid fixture')
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertTrue(self._send(4.10))
            self.assertFalse(self._send(4.18))
        self.assertIn('Telegram alert state read failed', stderr.getvalue())
        self.assertTrue(self._state()['event_conditions'][self.SHORT_EVENT]['active'])
        self.assertEqual(1, self.send_raw.call_count)

    def test_t10_transport_is_mocked_and_never_uses_network(self):
        with patch.object(telegram_alerts.urllib.request, 'urlopen') as urlopen:
            self.assertTrue(self._send(4.10))
        self.send_raw.assert_called_once()
        urlopen.assert_not_called()

    def test_write_failure_keeps_visibility_retry_without_trading_mutation(self):
        stderr = io.StringIO()
        with patch.object(telegram_alerts, '_write_state', return_value=False), \
             contextlib.redirect_stderr(stderr):
            self.assertTrue(self._send(4.10))
            self.assertTrue(self._send(4.18))
        self.assertEqual(2, self.send_raw.call_count)
        self.assertIn('state_persisted=false', stderr.getvalue())


class ObservableCapacityTests(unittest.TestCase):
    def test_dynamic_capacity_is_not_capped_by_static_config(self):
        self.assertEqual(bot_state._wallet_max_positions(100, configured_max=2, dynamic_value=4), 4)

    def test_zero_target_still_disables_capacity(self):
        self.assertEqual(bot_state._wallet_max_positions(0, configured_max=2, dynamic_value=4), 0)

    @patch.dict(os.environ, {'BOT_TOTAL_CAPITAL_LIMIT_USDT': '54'}, clear=False)
    def test_valid_existing_positions_distinguish_operational_and_target_capacity(self):
        positions = [
            {'symbol': 'XRPUSDT', 'direction': 'long', 'entry_price': 1, 'quantity': 10},
            {'symbol': 'ADAUSDT', 'direction': 'long', 'entry_price': 0.2, 'quantity': 50},
        ]
        payload = bot_state.build_bot_state(
            state={'positions': positions},
            btc_ctx={'trend': 'bullish', 'btc_price': 60000, 'change_4h': 1},
            spot_real=48, futures_real=3, spot_target=25, futures_target=26,
            max_longs=2, max_shorts=2,
        )
        capacity = payload['positions']['long']
        self.assertEqual(capacity['current'], 2)
        self.assertEqual(capacity['operational_max'], 2)
        self.assertEqual(capacity['target_max'], 1)
        self.assertFalse(capacity['new_entries_allowed'])
        self.assertEqual(capacity['capacity_status'], 'AT_CAPACITY')
        self.assertEqual(capacity['target_capacity_status'], 'OVER_TARGET_CAPACITY_NON_INCREMENTABLE')

    def test_explicit_operational_capacity_is_not_clamped_by_target(self):
        self.assertEqual(bot_state.compute_observable_max_positions(25, 25, 2, 2), (2, 2))

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
