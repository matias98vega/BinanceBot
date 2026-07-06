#!/usr/bin/env python3
import io
import os
import sys
import tempfile
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
import residuals
import shorts
import sl_guardian
import utils
from orchestration import position_lifecycle


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

    def test_orphan_residual_below_min_notional_does_not_create_oco(self):
        client = Mock()
        client.spot_ticker_prices.return_value = [{'symbol': 'SOLUSDT', 'price': '100'}]
        client.get_spot_account.return_value = {
            'balances': [{'asset': 'SOL', 'free': '0.06186400', 'locked': '0'}]
        }
        client.spot_signed.return_value = [{'isBuyer': True, 'price': '100'}]
        client.get_klines.side_effect = RuntimeError('klines unavailable')
        client.get_spot_filters.return_value = {'tick_size': 0.01, 'step_size': 0.000001, 'min_notional': 10.0}
        state = {'positions': []}

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(bot, 'BINANCE', client), \
             patch.object(residuals, 'DEFAULT_STATUS_FILE', os.path.join(tmp, 'residuals_status.json')), \
             patch('utils.get_active_cooldowns', return_value=set()), \
             patch('utils.send_alert') as send_alert, \
             patch('decision_timeline.record_event') as record_event, \
             patch.object(bot, 'out'), \
             self.assertLogs(level='WARNING') as logs:
            bot._audit_orphans(state)
            status = residuals.load_status(os.path.join(tmp, 'residuals_status.json'))

        self.assertEqual(client.spot_signed.call_count, 1)
        self.assertEqual(client.spot_signed.call_args.args[0], 'GET')
        alert = send_alert.call_args.args[0]
        self.assertIn('SOL residual sin OCO', alert)
        self.assertIn('orden OCO final queda bajo el mínimo', alert)
        self.assertIn('Valor estimado:', alert)
        self.assertIn('Notional TP:', alert)
        self.assertIn('Notional SL:', alert)
        self.assertIn('Pata limitante:', alert)
        self.assertIn('Mínimo requerido: 10.00 USDT', alert)
        self.assertIn('vender manualmente o acumular más saldo', alert)
        self.assertTrue(record_event.called)
        log_text = '\n'.join(logs.output)
        self.assertIn('RESIDUAL STATUS WRITE path=', log_text)
        self.assertIn('RESIDUAL UNPROTECTABLE', log_text)
        saved = status['residuals']['SOLUSDT']
        self.assertEqual(saved['status'], 'unprotectable_residual')
        self.assertEqual(saved['min_notional'], 10.0)

    def test_unprotectable_residual_status_is_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'residuals_status.json')
            entry, should_alert = residuals.classify_unprotectable_residual(
                'SOLUSDT',
                'SOL',
                0.061864,
                6.18,
                10.0,
                rounded_qty=0.061864,
                rounded_price=100.0,
                notional_after_rounding=6.1864,
                path=path,
            )
            data = residuals.load_status(path)

        saved = data['residuals']['SOLUSDT']
        self.assertTrue(should_alert)
        self.assertEqual(entry['status'], 'unprotectable_residual')
        self.assertEqual(saved['reason'], 'below_min_notional')
        self.assertEqual(saved['suggested_action'], 'vender manualmente o acumular mas saldo antes de proteger')

    def test_oco_leg_below_min_notional_is_unprotectable_even_if_estimated_value_is_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'residuals_status.json')
            handled = residuals.handle_unprotectable_spot_residual(
                'NEARUSDT',
                'NEAR',
                2.595,
                2.0,
                {'tick_size': 0.001, 'step_size': 0.001, 'min_qty': 0.001, 'min_notional': 5.0},
                limit_price=2.3,
                stop_price=1.93,
                stop_limit_price=1.925,
                path=path,
            )
            data = residuals.load_status(path)

        saved = data['residuals']['NEARUSDT']
        self.assertTrue(handled)
        self.assertEqual(saved['reason'], 'oco_leg_below_min_notional')
        self.assertGreater(saved['estimated_value'], saved['min_notional'])
        self.assertGreaterEqual(saved['limit_notional'], saved['min_notional'])
        self.assertLess(saved['stop_notional'], saved['min_notional'])
        self.assertEqual(saved['min_leg_notional'], saved['stop_notional'])
        self.assertEqual(saved['limiting_leg'], 'SL / stopLimitPrice')

    def test_oco_leg_notional_allows_current_flow_when_both_legs_are_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'residuals_status.json')
            handled = residuals.handle_unprotectable_spot_residual(
                'NEARUSDT',
                'NEAR',
                2.595,
                2.0,
                {'tick_size': 0.001, 'step_size': 0.001, 'min_qty': 0.001, 'min_notional': 5.0},
                limit_price=2.3,
                stop_price=1.94,
                stop_limit_price=1.93,
                path=path,
            )
            data = residuals.load_status(path)

        self.assertFalse(handled)
        self.assertEqual(data, {})

    def test_oco_leg_warning_message_is_not_contradictory(self):
        msg = residuals.residual_alert_message({
            'asset': 'NEAR',
            'symbol': 'NEARUSDT',
            'quantity': 2.595,
            'estimated_value': 5.19,
            'min_notional': 5.0,
            'limit_notional': 5.96,
            'stop_notional': 4.99,
            'limiting_leg': 'SL / stopLimitPrice',
            'reason': 'oco_leg_below_min_notional',
        })

        self.assertIn('una pata de la OCO queda bajo el mínimo', msg)
        self.assertIn('Valor estimado: 5.19 USDT', msg)
        self.assertIn('Notional TP: 5.96 USDT', msg)
        self.assertIn('Notional SL: 4.99 USDT', msg)
        self.assertIn('Pata limitante: SL / stopLimitPrice', msg)
        self.assertNotIn('valor queda por debajo', msg)

    def test_final_oco_payload_below_min_notional_is_unprotectable(self):
        payload = {
            'symbol': 'NEARUSDT',
            'side': 'SELL',
            'quantity': '2.5',
            'price': '2.039',
            'stopPrice': '1.999',
            'stopLimitPrice': '1.996',
            'stopLimitTimeInForce': 'GTC',
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'residuals_status.json')
            handled = residuals.handle_unprotectable_spot_residual(
                'NEARUSDT',
                'NEAR',
                2.595,
                2.02,
                {'tick_size': 0.001, 'step_size': 0.1, 'min_qty': 0.1, 'min_notional': 5.0},
                oco_payload=payload,
                path=path,
            )
            data = residuals.load_status(path)

        saved = data['residuals']['NEARUSDT']
        self.assertTrue(handled)
        self.assertEqual(saved['reason'], 'oco_payload_below_min_notional')
        self.assertEqual(saved['balance_quantity'], 2.595)
        self.assertEqual(saved['payload_quantity'], 2.5)
        self.assertAlmostEqual(saved['stop_notional'], 4.99)
        self.assertLess(saved['stop_notional'], saved['min_notional'])
        self.assertEqual(saved['limiting_leg'], 'SL')
        self.assertEqual(saved['raw_payload_sanitized']['quantity'], '2.5')

    def test_final_oco_payload_allows_current_flow_when_notional_is_valid(self):
        payload = {
            'symbol': 'NEARUSDT',
            'side': 'SELL',
            'quantity': '2.5',
            'price': '2.04',
            'stopPrice': '2.006',
            'stopLimitPrice': '2.005',
            'stopLimitTimeInForce': 'GTC',
        }
        result = residuals.validate_spot_oco_payload_notional(
            payload,
            {'min_notional': 5.0},
        )

        self.assertTrue(result['should_send_oco'])
        self.assertAlmostEqual(result['stop_notional'], 5.0125)

    def test_final_oco_payload_with_unrounded_quantity_can_be_valid(self):
        payload = {
            'symbol': 'NEARUSDT',
            'side': 'SELL',
            'quantity': '2.595',
            'price': '2.3',
            'stopPrice': '1.93',
            'stopLimitPrice': '1.928',
            'stopLimitTimeInForce': 'GTC',
        }
        result = residuals.validate_spot_oco_payload_notional(
            payload,
            {'min_notional': 5.0},
        )

        self.assertTrue(result['should_send_oco'])
        self.assertAlmostEqual(result['stop_notional'], 5.00316)

    def test_final_payload_warning_message_is_not_contradictory(self):
        msg = residuals.residual_alert_message({
            'asset': 'NEAR',
            'symbol': 'NEARUSDT',
            'quantity': 2.595,
            'balance_quantity': 2.595,
            'payload_quantity': 2.5,
            'estimated_value': 5.24,
            'min_notional': 5.0,
            'limit_notional': 5.10,
            'stop_notional': 4.99,
            'limiting_leg': 'SL',
            'reason': 'oco_payload_below_min_notional',
        })

        self.assertIn('orden OCO final queda bajo el mínimo', msg)
        self.assertIn('Cantidad balance: 2.59500000', msg)
        self.assertIn('Cantidad enviada: 2.50000000', msg)
        self.assertIn('Valor estimado: 5.24 USDT', msg)
        self.assertIn('Notional SL: 4.99 USDT', msg)
        self.assertIn('Pata limitante: SL', msg)
        self.assertNotIn('valor queda por debajo', msg)

    def test_unprotectable_residual_alert_is_throttled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'residuals_status.json')
            first, first_alert = residuals.classify_unprotectable_residual(
                'SOLUSDT', 'SOL', 0.061864, 6.18, 10.0, path=path
            )
            second, second_alert = residuals.classify_unprotectable_residual(
                'SOLUSDT', 'SOL', 0.061864, 6.18, 10.0, path=path
            )

        self.assertTrue(first_alert)
        self.assertFalse(second_alert)
        self.assertEqual(second['alert_count'], first['alert_count'])
        self.assertEqual(second['first_seen'], first['first_seen'])

    def test_long_recovery_without_oco_records_residual_before_post(self):
        client = Mock()
        client.get_spot_price.return_value = 100.0
        client.get_spot_filters.return_value = {'tick_size': 0.01, 'step_size': 0.000001, 'min_notional': 10.0}
        client.get_asset_spot.return_value = 0.061864
        pos = {
            'id': 'long_SOLUSDT_1',
            'symbol': 'SOLUSDT',
            'entry_price': 100.0,
            'quantity': 0.061864,
            'sl': 90.0,
            'tp': 120.0,
            'recovery_pending': True,
        }

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(longs, 'BINANCE', client), \
             patch.object(residuals, 'DEFAULT_STATUS_FILE', os.path.join(tmp, 'residuals_status.json')), \
             patch('utils.send_alert') as send_alert, \
             patch('decision_timeline.record_event'):
            action, price, pnl = longs._recolocar_oco(pos, {'positions': [pos]})
            status = residuals.load_status(os.path.join(tmp, 'residuals_status.json'))

        self.assertEqual(action, 'hold')
        self.assertEqual(price, 100.0)
        self.assertEqual(pnl, 0)
        client.spot_signed.assert_not_called()
        self.assertEqual(status['residuals']['SOLUSDT']['status'], 'unprotectable_residual')
        self.assertTrue(pos['recovery_pending'])
        self.assertIn('SOL residual sin OCO', send_alert.call_args.args[0])

    def test_long_recovery_uses_oco_leg_notional_before_post(self):
        client = Mock()
        client.get_spot_price.return_value = 2.0
        client.get_spot_filters.return_value = {
            'tick_size': 0.001,
            'step_size': 0.1,
            'min_qty': 0.1,
            'min_notional': 5.0,
        }
        client.get_asset_spot.return_value = 2.595
        pos = {
            'id': 'long_NEARUSDT_1',
            'symbol': 'NEARUSDT',
            'entry_price': 2.0,
            'quantity': 2.595,
            'sl': 1.998,
            'tp': 2.3,
            'recovery_pending': True,
        }

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(longs, 'BINANCE', client), \
             patch.object(residuals, 'DEFAULT_STATUS_FILE', os.path.join(tmp, 'residuals_status.json')), \
             patch('utils.send_alert') as send_alert, \
             patch('decision_timeline.record_event'):
            action, price, pnl = longs._recolocar_oco(pos, {'positions': [pos]})
            status = residuals.load_status(os.path.join(tmp, 'residuals_status.json'))

        self.assertEqual(action, 'hold')
        self.assertEqual(price, 2.0)
        self.assertEqual(pnl, 0)
        client.spot_signed.assert_not_called()
        saved = status['residuals']['NEARUSDT']
        self.assertEqual(saved['reason'], 'oco_payload_below_min_notional')
        self.assertEqual(saved['payload_quantity'], 2.5)
        self.assertEqual(saved['stop_limit_price'], 1.996)
        self.assertAlmostEqual(saved['stop_notional'], 4.99)
        self.assertLess(saved['stop_notional'], saved['min_notional'])
        self.assertIn('orden OCO final', send_alert.call_args.args[0])

    def test_position_lifecycle_recolocar_oco_detects_residual_before_post(self):
        client = Mock()
        client.get_spot_filters.return_value = {'tick_size': 0.01, 'step_size': 0.000001, 'min_notional': 10.0}
        pos = {'symbol': 'SOLUSDT'}

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(residuals, 'DEFAULT_STATUS_FILE', os.path.join(tmp, 'residuals_status.json')), \
             patch('utils.send_alert') as send_alert, \
             patch('decision_timeline.record_event'):
            position_lifecycle.recolocar_oco_long(
                pos,
                'SOLUSDT',
                0.061864,
                0.000001,
                100.0,
                120.0,
                100.0,
                client,
                Mock(),
            )
            status = residuals.load_status(os.path.join(tmp, 'residuals_status.json'))

        client.spot_signed.assert_not_called()
        self.assertEqual(status['residuals']['SOLUSDT']['status'], 'unprotectable_residual')
        self.assertIn('SOL residual sin OCO', send_alert.call_args.args[0])

    def test_residual_alert_is_classified_as_warning(self):
        msg = residuals.residual_alert_message({
            'asset': 'SOL',
            'symbol': 'SOLUSDT',
            'quantity': 0.061864,
            'estimated_value': 6.18,
            'min_notional': 10.0,
        })

        with patch('telegram_alerts.send_telegram_alert') as telegram_alert, \
             patch('subprocess.run'):
            utils.send_alert(msg)

        self.assertEqual(telegram_alert.call_args.args[0], 'WARNING')
        self.assertEqual(telegram_alert.call_args.kwargs.get('notification_type'), 'WARNING')

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
