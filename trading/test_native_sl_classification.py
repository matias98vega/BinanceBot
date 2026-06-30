#!/usr/bin/env python3
import io
import os
import sys
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import shorts


def http_error(body='{"code":-2021,"msg":"Order would immediately trigger."}'):
    err = urllib.error.HTTPError(
        url='https://fapi.binance.com/fapi/v1/order',
        code=400,
        msg='Bad Request',
        hdrs={},
        fp=io.BytesIO(body.encode('utf-8')),
    )
    err.binance_endpoint = '/fapi/v1/order'
    err.binance_method = 'POST'
    err.binance_payload = {
        'symbol': 'SUIUSDT',
        'side': 'BUY',
        'type': 'STOP_MARKET',
        'stopPrice': '0.706',
        'quantity': '31.5',
    }
    return err


class NativeSlClassificationTests(unittest.TestCase):
    @patch('telegram_alerts.send_telegram_alert')
    @patch('decision_timeline.record_protection_event')
    def test_native_sl_failure_with_guardian_fallback_is_warning(self, record_event, send_telegram):
        level, message, details = shorts._notify_native_sl_failure(
            'SUIUSDT', 0.706, 31.5, 0.6932, http_error(), fallback_active=True
        )

        self.assertEqual(level, 'WARNING')
        self.assertIn('Guardian software activo como fallback', message)
        self.assertEqual(details['status'], 400)
        self.assertEqual(details['code'], -2021)
        record_event.assert_called_once()
        self.assertEqual(record_event.call_args.kwargs['level'], 'WARNING')
        send_telegram.assert_called_once()
        self.assertEqual(send_telegram.call_args.args[0], 'WARNING')
        self.assertNotIn('CRITICAL', send_telegram.call_args.args)

    @patch('telegram_alerts.send_telegram_alert')
    @patch('decision_timeline.record_protection_event')
    def test_native_sl_failure_without_fallback_is_critical(self, record_event, send_telegram):
        level, message, _ = shorts._notify_native_sl_failure(
            'SUIUSDT', 0.706, 31.5, 0.6932, http_error(), fallback_active=False
        )

        self.assertEqual(level, 'CRITICAL')
        self.assertIn('no hay fallback activo', message)
        self.assertEqual(record_event.call_args.kwargs['level'], 'CRITICAL')
        self.assertEqual(send_telegram.call_args.args[0], 'CRITICAL')

    @patch('telegram_alerts.send_telegram_alert')
    @patch('decision_timeline.record_protection_event')
    def test_native_sl_failure_keeps_binance_details_in_logs(self, _record_event, _send_telegram):
        with self.assertLogs(level='ERROR') as logs:
            shorts._notify_native_sl_failure(
                'SUIUSDT', 0.706, 31.5, 0.6932, http_error(), fallback_active=True
            )

        text = '\n'.join(logs.output)
        self.assertIn('SUIUSDT', text)
        self.assertIn('stopPrice=0.706', text)
        self.assertIn('qty=31.5', text)
        self.assertIn('price=0.6932', text)
        self.assertIn("'status': 400", text)
        self.assertIn("'code': -2021", text)
        self.assertIn('Order would immediately trigger', text)
        self.assertIn('raw_body', text)


if __name__ == '__main__':
    unittest.main()
