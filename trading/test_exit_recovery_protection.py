#!/usr/bin/env python3
import io
import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError

os.environ.setdefault('BINANCE_API_KEY', 'test')
os.environ.setdefault('BINANCE_API_SECRET', 'test')

sys.path.insert(0, os.path.dirname(__file__))

import bot
import longs
import shorts
import sl_guardian
import utils


def _http_error(body, code=400):
    return HTTPError(
        url='https://api.binance.test/order',
        code=code,
        msg='Bad Request',
        hdrs={},
        fp=io.BytesIO(body.encode('utf-8')),
    )


class ExitRecoveryProtectionTests(unittest.TestCase):
    def test_manage_long_detects_tp_from_filled_oco(self):
        client = Mock()
        client.spot_signed.side_effect = [
            {'listOrderStatus': 'ALL_DONE', 'orders': [{'orderId': 11}]},
            {
                'status': 'FILLED',
                'executedQty': '10',
                'cummulativeQuoteQty': '110',
                'type': 'LIMIT_MAKER',
            },
        ]
        pos = {
            'symbol': 'ETHUSDT',
            'entry_price': 10.0,
            'quantity': 10.0,
            'oco_order_list_id': '99',
        }

        with patch.object(longs, 'BINANCE', client):
            action, price, pnl = longs.manage_long(pos, {'positions': [pos]})

        self.assertEqual(action, 'closed_tp')
        self.assertEqual(price, 11.0)
        self.assertGreater(pnl, 0)

    def test_manage_long_detects_sl_from_filled_oco(self):
        client = Mock()
        client.spot_signed.side_effect = [
            {'listOrderStatus': 'ALL_DONE', 'orders': [{'orderId': 12}]},
            {
                'status': 'FILLED',
                'executedQty': '10',
                'cummulativeQuoteQty': '90',
                'type': 'STOP_LOSS_LIMIT',
            },
        ]
        pos = {
            'symbol': 'ETHUSDT',
            'entry_price': 10.0,
            'quantity': 10.0,
            'oco_order_list_id': '99',
        }

        with patch.object(longs, 'BINANCE', client):
            action, price, pnl = longs.manage_long(pos, {'positions': [pos]})

        self.assertEqual(action, 'closed_sl')
        self.assertEqual(price, 9.0)
        self.assertLess(pnl, 0)

    def test_check_partial_long_sells_half_and_reprotects_rest(self):
        client = Mock()
        client.get_spot_price.return_value = 11.0
        client.get_spot_filters.return_value = {'step_size': 0.1, 'tick_size': 0.01}
        client.get_spot_account.return_value = {'balances': [{'asset': 'ETH', 'free': '10.0'}]}
        client.spot_signed.side_effect = [
            {},  # cancel OCO
            {'executedQty': '5.0', 'cummulativeQuoteQty': '55.0'},  # partial sell
            {'orderListId': 222, 'orders': [{'orderId': 333}]},  # new OCO
        ]
        pos = {
            'id': 'long_ETHUSDT_1',
            'direction': 'long',
            'symbol': 'ETHUSDT',
            'entry_price': 10.0,
            'quantity': 10.0,
            'tp': 12.0,
            'sl': 9.0,
            'oco_order_list_id': '111',
            'entry_time': 1,
        }
        state = {'total_pnl_usdt': 0.0, 'daily_pnl_usdt': 0.0}

        with patch.object(bot, 'BINANCE', client), \
             patch.object(bot, 'ANALYTICS') as analytics, \
             patch('utils.send_alert'), \
             patch('utils.format_trade_close_alert', return_value='partial alert'):
            bot._check_partial_long(pos, state)

        self.assertTrue(pos['partial_taken'])
        self.assertEqual(pos['quantity'], 5.0)
        self.assertEqual(pos['oco_order_list_id'], '222')
        self.assertEqual(state['total_pnl_usdt'], 5.0)
        analytics.log_trade_close.assert_called_once()
        sell_params = client.spot_signed.call_args_list[1].args[2]
        self.assertEqual(sell_params['quantity'], '5.0')

    def test_manage_long_stale_exit_closes_market(self):
        client = Mock()
        client.spot_signed.return_value = {'listOrderStatus': 'EXECUTING'}
        client.get_spot_price.return_value = 10.1
        pos = {
            'symbol': 'ETHUSDT',
            'entry_price': 10.0,
            'quantity': 2.0,
            'tp': 12.0,
            'sl': 9.0,
            'oco_order_list_id': '123',
            'entry_time': int(time.time() - 99 * 3600),
        }

        with patch.object(longs, 'BINANCE', client), \
             patch('config.STALE_MAX_HOURS', 12), \
             patch.object(longs, '_market_sell', return_value=(20.2, 10.1)) as market_sell:
            action, price, pnl = longs.manage_long(pos, {'positions': [pos]})

        self.assertEqual(action, 'closed_manual')
        self.assertEqual(price, 10.1)
        self.assertGreater(pnl, 0)
        market_sell.assert_called_once_with('ETHUSDT', 2.0)

    def test_recovery_long_without_oco_recreates_protection(self):
        client = Mock()
        client.get_spot_price.return_value = 10.0
        client.get_spot_filters.return_value = {
            'tick_size': 0.01,
            'step_size': 0.1,
            'min_qty': 0.1,
            'min_notional': 1.0,
        }
        client.get_asset_spot.return_value = 2.0
        client.spot_signed.return_value = {'orderListId': 555, 'orders': [{'orderId': 556}]}
        pos = {
            'id': 'long_ETHUSDT_1',
            'symbol': 'ETHUSDT',
            'entry_price': 10.0,
            'quantity': 2.0,
            'sl': 9.0,
            'tp': 12.0,
            'oco_order_list_id': '',
            'recovery_pending': True,
        }

        with patch.object(longs, 'BINANCE', client), \
             patch('utils.send_alert'), \
             patch('decision_timeline.record_protection_event'):
            action, price, _ = longs.manage_long(pos, {'positions': [pos]})

        self.assertEqual(action, 'updated')
        self.assertEqual(price, 10.0)
        self.assertEqual(pos['oco_order_list_id'], '555')
        self.assertFalse(pos['recovery_pending'])

    def test_guardian_closes_long_when_software_sl_is_reached(self):
        state = {
            'positions': [{
                'id': 'long_ETHUSDT_1',
                'direction': 'long',
                'symbol': 'ETHUSDT',
                'entry_price': 10.0,
                'quantity': 2.0,
                'sl': 9.0,
                'oco_order_list_id': '',
            }],
            'trade_count': 0,
            'total_pnl_usdt': 0.0,
            'daily_pnl_usdt': 0.0,
        }
        client = Mock()
        client.get_spot_price.return_value = 8.9

        with patch.object(sl_guardian, 'BINANCE', client), \
             patch('utils.load_state', return_value=state), \
             patch('utils.save_state') as save_state, \
             patch('utils.send_alert'), \
             patch('utils.add_cooldown'), \
             patch.object(sl_guardian, '_close_spot_market', return_value=('closed', 2.0)), \
             patch.object(sl_guardian, 'ANALYTICS'), \
             patch('decision_timeline.record_guardian_event'):
            sl_guardian._run()

        self.assertEqual(state['positions'], [])
        self.assertEqual(state['trade_count'], 1)
        save_state.assert_called_once_with(state)

    def test_guardian_skips_long_with_active_oco(self):
        state = {
            'positions': [{
                'id': 'long_ETHUSDT_1',
                'direction': 'long',
                'symbol': 'ETHUSDT',
                'entry_price': 10.0,
                'quantity': 2.0,
                'sl': 9.0,
                'oco_order_list_id': '123',
            }]
        }
        client = Mock()

        with patch.object(sl_guardian, 'BINANCE', client), \
             patch('utils.load_state', return_value=state), \
             patch('utils.save_state') as save_state, \
             patch('decision_timeline.record_guardian_event'), \
             patch('builtins.print'):
            sl_guardian._run()

        client.get_spot_price.assert_not_called()
        save_state.assert_not_called()

    def test_orphan_position_is_detected_and_reconciled_with_oco(self):
        client = Mock()
        client.spot_ticker_prices.return_value = [{'symbol': 'ETHUSDT', 'price': '10'}]
        client.get_spot_account.return_value = {
            'balances': [{'asset': 'ETH', 'free': '1.0', 'locked': '0'}]
        }
        client.spot_signed.side_effect = [
            [{'isBuyer': True, 'price': '10.5'}],
            {'orderListId': 777, 'orders': [{'orderId': 778}]},
        ]
        client.get_klines.side_effect = RuntimeError('klines unavailable')
        client.get_spot_filters.return_value = {'tick_size': 0.01, 'step_size': 0.1}
        state = {'positions': []}

        with patch.object(bot, 'BINANCE', client), \
             patch('utils.get_active_cooldowns', return_value=set()), \
             patch('utils.send_alert') as send_alert, \
             patch('utils.save_state') as save_state, \
             patch.object(bot, '_safe_log_open'), \
             patch.object(bot, 'out'):
            bot._audit_orphans(state)

        self.assertEqual(len(state['positions']), 1)
        self.assertEqual(state['positions'][0]['symbol'], 'ETHUSDT')
        self.assertEqual(state['positions'][0]['oco_order_list_id'], '777')
        save_state.assert_called_once_with(state)
        self.assertGreaterEqual(send_alert.call_count, 2)

    def test_orphan_position_is_reported_when_reconciliation_fails(self):
        client = Mock()
        client.spot_ticker_prices.return_value = [{'symbol': 'ETHUSDT', 'price': '10'}]
        client.get_spot_account.return_value = {
            'balances': [{'asset': 'ETH', 'free': '1.0', 'locked': '0'}]
        }
        client.spot_signed.side_effect = [
            [{'isBuyer': True, 'price': '9.5'}],
            RuntimeError('oco rejected'),
        ]
        client.get_klines.side_effect = RuntimeError('klines unavailable')
        client.get_spot_filters.return_value = {'tick_size': 0.01, 'step_size': 0.1}
        state = {'positions': []}

        with patch.object(bot, 'BINANCE', client), \
             patch('utils.get_active_cooldowns', return_value=set()), \
             patch('utils.send_alert') as send_alert, \
             patch('utils.save_state') as save_state, \
             patch.object(bot, 'out'):
            bot._audit_orphans(state)

        self.assertEqual(state['positions'], [])
        save_state.assert_not_called()
        messages = [call.args[0] for call in send_alert.call_args_list]
        self.assertTrue(any('sin OCO' in msg for msg in messages))

    def test_orphan_oco_http_error_alert_includes_binance_details(self):
        raw_body = '{"code":-2010,"msg":"Account has insufficient balance for requested action."}'
        err = _http_error(raw_body)
        err.binance_endpoint = '/api/v3/order/oco'
        err.binance_method = 'POST'
        client = Mock()
        client.spot_ticker_prices.return_value = [{'symbol': 'SOLUSDT', 'price': '10'}]
        client.get_spot_account.return_value = {
            'balances': [{'asset': 'SOL', 'free': '1.0', 'locked': '0'}]
        }
        client.spot_signed.side_effect = [
            [{'isBuyer': True, 'price': '10.5'}],
            err,
        ]
        client.get_klines.side_effect = RuntimeError('klines unavailable')
        client.get_spot_filters.return_value = {'tick_size': 0.01, 'step_size': 0.1}
        state = {'positions': []}

        with patch.object(bot, 'BINANCE', client), \
             patch('utils.get_active_cooldowns', return_value=set()), \
             patch('utils.send_alert') as send_alert, \
             patch('utils.save_state'), \
             patch.object(bot, 'out'), \
             self.assertLogs(level='ERROR') as logs:
            bot._audit_orphans(state)

        alert = send_alert.call_args_list[-1].args[0]
        self.assertIn('🚨', alert)
        self.assertIn('huérfano sin OCO', alert)
        self.assertIn('Requiere intervención manual', alert)
        self.assertIn('code=-2010', alert)
        self.assertIn('Account has insufficient balance', alert)
        self.assertIn(raw_body, alert)
        log_text = '\n'.join(logs.output)
        self.assertIn('operation=orphan spot OCO recovery', log_text)
        self.assertIn('endpoint=/api/v3/order/oco', log_text)
        self.assertIn('status=400', log_text)
        self.assertIn('code=-2010', log_text)
        self.assertIn(raw_body, log_text)

    def test_orphan_oco_http_error_alert_handles_missing_json_body(self):
        err = _http_error('', code=400)
        client = Mock()
        client.spot_ticker_prices.return_value = [{'symbol': 'SOLUSDT', 'price': '10'}]
        client.get_spot_account.return_value = {
            'balances': [{'asset': 'SOL', 'free': '1.0', 'locked': '0'}]
        }
        client.spot_signed.side_effect = [
            [{'isBuyer': True, 'price': '10.5'}],
            err,
        ]
        client.get_klines.side_effect = RuntimeError('klines unavailable')
        client.get_spot_filters.return_value = {'tick_size': 0.01, 'step_size': 0.1}
        state = {'positions': []}

        with patch.object(bot, 'BINANCE', client), \
             patch('utils.get_active_cooldowns', return_value=set()), \
             patch('utils.send_alert') as send_alert, \
             patch('utils.save_state'), \
             patch.object(bot, 'out'):
            bot._audit_orphans(state)

        alert = send_alert.call_args_list[-1].args[0]
        self.assertIn('SOL huérfano sin OCO', alert)
        self.assertIn('HTTP 400', alert)
        self.assertNotIn('ðŸ', alert)
        self.assertNotIn('huÃ', alert)

    def test_http_400_preserves_code_msg_and_raw_body(self):
        raw_body = '{"code":-1013,"msg":"Filter failure: LOT_SIZE"}'
        err = _http_error(raw_body)
        err.binance_endpoint = '/fapi/v1/order'
        err.binance_method = 'POST'
        err.binance_payload = {'symbol': 'ETHUSDT', 'quantity': '0.0001'}

        details = utils.extract_http_error_details(err)

        self.assertEqual(details['status'], 400)
        self.assertEqual(details['code'], -1013)
        self.assertEqual(details['msg'], 'Filter failure: LOT_SIZE')
        self.assertEqual(details['raw_body'], raw_body)
        self.assertEqual(details['endpoint'], '/fapi/v1/order')
        self.assertEqual(details['method'], 'POST')
        self.assertEqual(details['payload']['symbol'], 'ETHUSDT')

    def test_native_sl_failure_warning_and_critical_levels_are_preserved(self):
        with patch('telegram_alerts.send_telegram_alert'), \
             patch('decision_timeline.record_protection_event'):
            warning_level, warning_msg, _ = shorts._notify_native_sl_failure(
                'SUIUSDT',
                0.706,
                31.5,
                0.6932,
                _http_error('{"code":-2021,"msg":"Order would immediately trigger."}'),
                fallback_active=True,
            )
            critical_level, critical_msg, _ = shorts._notify_native_sl_failure(
                'SUIUSDT',
                0.706,
                31.5,
                0.6932,
                _http_error('{"code":-2021,"msg":"Order would immediately trigger."}'),
                fallback_active=False,
            )

        self.assertEqual(warning_level, 'WARNING')
        self.assertIn('Guardian software activo', warning_msg)
        self.assertEqual(critical_level, 'CRITICAL')
        self.assertIn('no hay fallback activo', critical_msg)


if __name__ == '__main__':
    unittest.main()
