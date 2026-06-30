#!/usr/bin/env python3
import os
import sys
import io
import json
import tempfile
import urllib.error
import unittest
from unittest.mock import patch

os.environ.setdefault('BINANCE_API_KEY', 'test')
os.environ.setdefault('BINANCE_API_SECRET', 'test')

sys.path.insert(0, os.path.dirname(__file__))

import rebalance
import telegram_commands


def http_error(status=400, body='{"code":-2010,"msg":"Insufficient balance"}'):
    err = urllib.error.HTTPError(
        url='https://api.binance.com/sapi/v1/asset/transfer',
        code=status,
        msg='Bad Request',
        hdrs={},
        fp=io.BytesIO(body.encode('utf-8')),
    )
    err.binance_endpoint = '/sapi/v1/asset/transfer'
    err.binance_method = 'POST'
    err.binance_payload = {'type': 'MAIN_UMFUTURE', 'asset': 'USDT', 'amount': '26.94'}
    return err


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


class RebalanceDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.status_file = os.path.join(self.tmpdir.name, 'rebalance_status.json')
        self.status_patch = patch.object(rebalance, 'REBALANCE_STATUS_FILE', self.status_file)
        self.status_patch.start()

    def tearDown(self):
        self.status_patch.stop()
        self.tmpdir.cleanup()

    def read_status(self):
        with open(self.status_file, encoding='utf-8') as f:
            return json.load(f)

    def test_persists_rebalance_failure_details(self):
        with patch.object(rebalance.decision_timeline, 'record_rebalance_event') as timeline:
            status, details = rebalance._record_rebalance_failure(
                'SPOT_TO_FUTURES',
                26.94,
                http_error(),
                {'type': 'MAIN_UMFUTURE', 'asset': 'USDT', 'amount': '26.94'},
            )

        saved = self.read_status()
        self.assertTrue(saved['pending'])
        self.assertEqual(saved['direction'], 'SPOT_TO_FUTURES')
        self.assertEqual(saved['amount'], 26.94)
        self.assertEqual(saved['attempts'], 1)
        self.assertEqual(saved['last_http_status'], 400)
        self.assertEqual(saved['last_binance_code'], -2010)
        self.assertEqual(saved['last_message'], 'Insufficient balance')
        self.assertIn('Insufficient balance', saved['last_raw_body'])
        self.assertEqual(details['payload']['amount'], '26.94')
        timeline.assert_called()
        args, kwargs = timeline.call_args
        self.assertEqual(args[0], 'rebalance_error')
        self.assertIn('intento #1', args[1])
        self.assertIn('Insufficient balance', args[1])
        self.assertEqual(kwargs['details']['attempts'], 1)
        self.assertEqual(kwargs['details']['binance_code'], -2010)

    def test_failure_attempts_increment(self):
        with patch.object(rebalance.decision_timeline, 'record_rebalance_event'):
            rebalance._record_rebalance_failure('SPOT_TO_FUTURES', 26.94, http_error(), {})
            rebalance._record_rebalance_failure('SPOT_TO_FUTURES', 26.94, http_error(), {})

        self.assertEqual(self.read_status()['attempts'], 2)

    def test_clear_rebalance_status_after_success(self):
        with patch.object(rebalance.decision_timeline, 'record_rebalance_event'):
            rebalance._record_rebalance_failure('SPOT_TO_FUTURES', 26.94, http_error(), {})

        cleared = rebalance.clear_rebalance_status()

        self.assertFalse(cleared['pending'])
        self.assertFalse(self.read_status()['pending'])
        self.assertEqual(self.read_status()['attempts'], 0)

    def test_error_message_preserves_binance_code_and_msg(self):
        with patch.object(rebalance.decision_timeline, 'record_rebalance_event'):
            _, details = rebalance._record_rebalance_failure('SPOT_TO_FUTURES', 26.94, http_error(), {})

        message = rebalance._format_transfer_error('SPOT_TO_FUTURES', details, RuntimeError('HTTP Error 400'))

        self.assertIn('HTTP 400', message)
        self.assertIn('code=-2010', message)
        self.assertIn('Insufficient balance', message)

    def test_telegram_capital_shows_rebalance_reason(self):
        metrics = {
            'total_real': 54.0,
            'total_limit': 54.0,
            'total_authorized': 54.0,
            'spot_real': 26.9,
            'spot_target': 0.0,
            'spot_used': 8.4,
            'spot_reserved': 0,
            'futures_real': 27.1,
            'futures_target': 54.0,
            'futures_used': 18.2,
            'futures_reserved': 0,
            'rebalance': {
                'status': 'PENDING',
                'direction': 'SPOT_TO_FUTURES',
                'amount_pending': 26.94,
                'attempts': 17,
                'last_attempt': '2026-06-30T18:32:00Z',
                'last_http_status': 400,
                'last_binance_code': -2010,
                'last_message': 'Insufficient balance',
            },
            'max_exposure_percent': 80.0,
            'max_position_percent': None,
            'warning': None,
        }

        with patch.object(telegram_commands, '_exposure_metrics', return_value=metrics):
            text = telegram_commands._render_page('capital')['text']

        self.assertIn('Rebalance pendiente', text)
        self.assertIn('Dirección:\nSpot → Futures', text)
        self.assertIn('Monto:\n26.94 USDT', text)
        self.assertIn('Intentos:\n17', text)
        self.assertIn('HTTP 400', text)
        self.assertIn('code=-2010', text)
        self.assertIn('Insufficient balance', text)


if __name__ == '__main__':
    unittest.main()
