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


if __name__ == '__main__':
    unittest.main()
