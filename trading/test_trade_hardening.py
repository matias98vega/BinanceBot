#!/usr/bin/env python3
import io
import os
import sys
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

os.environ.setdefault('BINANCE_API_KEY', 'test')
os.environ.setdefault('BINANCE_API_SECRET', 'test')

sys.path.insert(0, os.path.dirname(__file__))

import longs
import utils


def _http_error(body):
    return HTTPError(
        url='https://api.binance.test',
        code=400,
        msg='Bad Request',
        hdrs={},
        fp=io.BytesIO(body.encode('utf-8')),
    )


class TradeHardeningTests(unittest.TestCase):
    def test_capacity_reject_short_at_limit(self):
        state = {'positions': [{'direction': 'short'}, {'direction': 'short'}]}
        ok, msg, count, limit = utils.validate_position_capacity(state, 'short', 2)
        self.assertFalse(ok)
        self.assertEqual(count, 2)
        self.assertEqual(limit, 2)
        self.assertEqual(msg, 'CAPACITY LIMIT REJECT: shorts 2/2')

    def test_capacity_reject_long_at_limit(self):
        state = {'positions': [{'direction': 'long'}]}
        ok, msg, count, limit = utils.validate_position_capacity(state, 'long', 1)
        self.assertFalse(ok)
        self.assertEqual(count, 1)
        self.assertEqual(limit, 1)
        self.assertEqual(msg, 'CAPACITY LIMIT REJECT: longs 1/1')

    def test_http_400_preserves_binance_code_msg(self):
        details = utils.extract_http_error_details(
            _http_error('{"code":-2010,"msg":"Order would immediately trigger."}')
        )
        self.assertEqual(details['status'], 400)
        self.assertEqual(details['code'], -2010)
        self.assertEqual(details['msg'], 'Order would immediately trigger.')

    @patch('utils.get_spot_price', return_value=1.0)
    @patch('utils.get_spot_filters', return_value={'tick_size': 0.0001})
    @patch('utils.send_alert')
    @patch('utils.spot_signed')
    def test_recovery_oco_success_does_not_send_critical(self, spot_signed, send_alert, *_):
        spot_signed.return_value = {'orderListId': 123, 'orders': [{'orderId': 456}]}
        pos = {
            'symbol': 'SUIUSDT',
            'sl': 0.9,
            'tp': 1.1,
            'quantity': 10,
            'entry_price': 1.0,
            'oco_order_list_id': '',
        }
        action, _, _ = longs._recolocar_oco(pos, {'positions': [pos]})
        self.assertEqual(action, 'updated')
        self.assertEqual(pos['oco_order_list_id'], '123')
        messages = [call.args[0] for call in send_alert.call_args_list]
        self.assertTrue(messages)
        self.assertFalse(any('🚨' in msg for msg in messages))

    @patch('utils.get_spot_price', return_value=1.0)
    @patch('utils.get_spot_filters', return_value={'tick_size': 0.0001})
    @patch('utils.send_alert')
    @patch('utils.spot_signed', side_effect=RuntimeError('oco rejected'))
    def test_recovery_oco_failure_sends_critical(self, _spot_signed, send_alert, *_):
        pos = {
            'symbol': 'SUIUSDT',
            'sl': 0.9,
            'tp': 1.1,
            'quantity': 10,
            'entry_price': 1.0,
            'oco_order_list_id': '',
        }
        action, _, _ = longs._recolocar_oco(pos, {'positions': [pos]})
        self.assertEqual(action, 'hold')
        messages = [call.args[0] for call in send_alert.call_args_list]
        self.assertTrue(any('🚨' in msg for msg in messages))


if __name__ == '__main__':
    unittest.main()
