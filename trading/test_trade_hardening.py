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
import sl_guardian
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
    @patch('utils.get_asset_spot', return_value=10.0)
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
    @patch('utils.get_asset_spot', return_value=10.0)
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

    @patch('utils.get_spot_filters', return_value={
        'step_size': 0.1, 'min_qty': 0.1, 'min_notional': 1.0, 'tick_size': 0.0001
    })
    @patch('utils.get_asset_spot', return_value=9.5)
    def test_adjust_spot_qty_uses_real_balance_not_theoretical_qty(self, *_):
        qty, free_balance = longs._adjust_spot_qty('XRPUSDT', 10.0, price=1.0)
        self.assertEqual(qty, 9.5)
        self.assertEqual(free_balance, 9.5)

    @patch('time.sleep')
    @patch('config.OCO_MAX_RETRIES', 1)
    @patch('config.DRY_RUN', False)
    @patch('capital_manager.validate_spot_order', return_value=(True, 'OK', 10.0))
    @patch('utils.get_spot_capital_per_position', return_value=10.0)
    @patch('utils.get_spot_risk_pct', return_value=1.0)
    @patch('utils.get_usdt_spot', return_value=100.0)
    @patch('utils.get_spot_price', return_value=1.0)
    @patch('utils.get_spot_filters', return_value={
        'step_size': 0.1, 'min_qty': 0.1, 'min_notional': 1.0, 'tick_size': 0.0001
    })
    @patch('utils.log_binance_http_error', wraps=utils.log_binance_http_error)
    @patch('utils.get_asset_spot', side_effect=[9.5, 9.4])
    @patch('utils.spot_signed')
    def test_open_long_retries_oco_with_adjusted_real_balance(
        self, spot_signed, _asset, _log_error, *_,
    ):
        spot_signed.side_effect = [
            {'executedQty': '10.0', 'cummulativeQuoteQty': '10.0'},
            _http_error('{"code":-2010,"msg":"Account has insufficient balance for requested action."}'),
            {'orderListId': 123, 'orders': [{'orderId': 456}]},
        ]
        candidate = {'symbol': 'XRPUSDT', 'sl': 0.9, 'tp': 1.1, 'atr': 0.01}

        pos, msg = longs.open_long(candidate, {'positions': []}, max_longs=1)

        self.assertIsNotNone(pos, msg)
        self.assertEqual(pos['quantity'], 9.4)
        self.assertEqual(pos['oco_order_list_id'], '123')
        first_oco_params = spot_signed.call_args_list[1].args[2]
        retry_oco_params = spot_signed.call_args_list[2].args[2]
        self.assertEqual(first_oco_params['quantity'], '9.5')
        self.assertEqual(retry_oco_params['quantity'], '9.4')

    @patch('utils.get_spot_price', return_value=1.0)
    @patch('utils.get_spot_filters', return_value={
        'step_size': 0.1, 'min_qty': 0.1, 'min_notional': 1.0, 'tick_size': 0.0001
    })
    @patch('utils.get_asset_spot', return_value=4.2)
    @patch('utils.spot_signed', return_value={'executedQty': '4.2', 'cummulativeQuoteQty': '4.2'})
    def test_emergency_market_sell_uses_min_state_qty_and_free_balance(self, spot_signed, *_):
        sold_quote, fill_price = longs._market_sell('ETHUSDT', 5.0, price=1.0)
        params = spot_signed.call_args.args[2]
        self.assertEqual(params['quantity'], '4.2')
        self.assertEqual(sold_quote, 4.2)
        self.assertEqual(fill_price, 1.0)

    @patch('utils.get_spot_filters', return_value={
        'step_size': 0.1, 'min_qty': 0.1, 'min_notional': 1.0
    })
    @patch('utils.get_asset_spot', return_value=0.0)
    def test_guardian_cleans_state_when_long_balance_is_zero(self, *_):
        status, closed_qty = sl_guardian._close_spot_market('ETHUSDT', 5.0, 1.0)
        self.assertEqual(status, 'already_closed')
        self.assertEqual(closed_qty, 0.0)

    @patch('utils.get_spot_filters', return_value={
        'step_size': 0.1, 'min_qty': 0.1, 'min_notional': 1.0
    })
    @patch('utils.get_asset_spot', return_value=3.0)
    @patch('utils.send_alert')
    @patch('utils.spot_signed', side_effect=_http_error(
        '{"code":-2010,"msg":"Account has insufficient balance for requested action."}'
    ))
    def test_guardian_alerts_critical_when_balance_exists_and_sell_fails(
        self, _spot_signed, send_alert, *_,
    ):
        status, closed_qty = sl_guardian._close_spot_market('ETHUSDT', 5.0, 1.0)
        self.assertEqual(status, 'failed')
        self.assertEqual(closed_qty, 0.0)
        self.assertTrue(send_alert.called)
        self.assertIn('Guardian no pudo cerrar LARGO', send_alert.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
