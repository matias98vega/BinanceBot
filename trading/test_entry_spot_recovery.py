#!/usr/bin/env python3
"""Offline entry-protection and emergency-exit recovery contracts."""

import json
import os
import sys
import time
import unittest
from decimal import Decimal
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(__file__))

import entry_spot_recovery
import longs
import partial_spot_long
import preventive_spot_close
import sl_guardian
from orchestration import cycle_runner
from spot_recovery_lock import is_spot_long_recovery_pending, spot_long_recovery_kind


class OfflineEntryClient:
    def __init__(self, *, extra='0.00002783', sell_mode='rejected'):
        self.managed = Decimal('0.00016')
        self.extra = Decimal(extra)
        self.total = self.extra
        self.price = 100000.0
        self.sell_mode = sell_mode
        self.sell_calls = []
        self.oco_calls = []
        self.buy_order = None
        self.sell_order = None
        self.oco = None
        self.initial_oco_failed = False
        self.restore_error = False
        self.query_error = False
        self.filters = {
            'status': 'TRADING', 'step_size': 0.00001, 'min_qty': 0.00001,
            'max_qty': '100', 'market_step_size': '0.00001',
            'market_min_qty': '0.00001', 'market_max_qty': '100',
            'min_notional': 5, 'tick_size': 0.01,
        }

    def get_spot_price(self, _symbol):
        return self.price

    def get_usdt_spot(self):
        return 100.0

    def get_spot_filters(self, _symbol):
        return dict(self.filters)

    def get_asset_spot(self, _asset):
        return float(self.total - (Decimal(self.oco['quantity']) if self.oco else 0))

    def get_spot_account(self):
        locked = Decimal(self.oco['quantity']) if self.oco else Decimal('0')
        return {'balances': [{'asset': 'BTC', 'free': str(self.total - locked),
                              'locked': str(locked)}]}

    def spot_open_orders(self, _params):
        if self.oco:
            return [dict(row) for row in self.oco['orders']]
        return []

    def get_order_list(self, _params):
        if not self.oco:
            raise RuntimeError('no OCO')
        return {'orderListId': 900, 'listOrderStatus': 'EXECUTING',
                'orders': [{'orderId': 901}, {'orderId': 902}]}

    def get_spot_order(self, params):
        if self.query_error:
            raise TimeoutError('offline GET timeout')
        if params.get('orderId') == 100:
            return dict(self.buy_order)
        if params.get('origClientOrderId') == (self.sell_order or {}).get('clientOrderId'):
            return dict(self.sell_order)
        raise RuntimeError('order not found')

    def set_sell(self, status, executed='0'):
        executed = Decimal(executed)
        self.total -= executed
        self.sell_order = {
            'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET',
            'status': status, 'orderId': 200,
            'clientOrderId': self.sell_calls[0]['newClientOrderId'],
            'origQty': self.sell_calls[0]['quantity'],
            'executedQty': str(executed),
            'cummulativeQuoteQty': str(executed * Decimal('100000')),
        }

    def create_spot_order(self, params):
        return self.spot_signed('POST', '/api/v3/order', params)

    def create_oco(self, params):
        return self.spot_signed('POST', '/api/v3/order/oco', params)

    def spot_signed(self, method, path, params):
        if method != 'POST':
            raise AssertionError('unexpected exchange operation')
        if path == '/api/v3/order' and params['side'] == 'BUY':
            self.total += self.managed
            self.buy_order = {
                'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET',
                'status': 'FILLED', 'orderId': 100, 'executedQty': str(self.managed),
                'cummulativeQuoteQty': '16',
            }
            return dict(self.buy_order)
        if path == '/api/v3/order' and params['side'] == 'SELL':
            self.sell_calls.append(dict(params))
            if self.sell_mode == 'timeout_unknown':
                raise TimeoutError('offline POST timeout')
            if self.sell_mode == 'timeout_filled':
                self.set_sell('FILLED', str(self.managed))
                raise TimeoutError('offline POST timeout after fill')
            self.set_sell('REJECTED')
            return dict(self.sell_order)
        if path == '/api/v3/order/oco':
            self.oco_calls.append(dict(params))
            if not self.initial_oco_failed:
                self.initial_oco_failed = True
                raise RuntimeError('offline initial OCO failure')
            if self.restore_error:
                raise RuntimeError('offline restore failure')
            quantity = params['quantity']
            self.oco = {
                'quantity': quantity,
                'orders': [
                    {'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'LIMIT_MAKER',
                     'orderListId': 900, 'orderId': 901, 'origQty': quantity,
                     'executedQty': '0', 'price': params['price'], 'stopPrice': '0'},
                    {'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'STOP_LOSS_LIMIT',
                     'orderListId': 900, 'orderId': 902, 'origQty': quantity,
                     'executedQty': '0', 'price': params['stopLimitPrice'],
                     'stopPrice': params['stopPrice']},
                ],
            }
            return {'orderListId': 900, 'orders': [{'orderId': 901}, {'orderId': 902}]}
        raise AssertionError(f'unexpected exchange operation {method} {path}')


class EntrySpotRecoveryTests(unittest.TestCase):
    def open_entry(self, *, sell_mode='rejected', extra='0.00002783'):
        client = OfflineEntryClient(extra=extra, sell_mode=sell_mode)
        candidate = {'symbol': 'BTCUSDT', 'sl': 80000.0, 'tp': 120000.0, 'atr': 1000.0}
        with patch.object(longs, 'BINANCE', client), \
             patch('config.DRY_RUN', False), \
             patch('config.OCO_MAX_RETRIES', 1), \
             patch('time.sleep'), \
             patch('utils.get_usdt_spot', return_value=100.0), \
             patch('utils.get_spot_risk_pct', return_value=1.0), \
             patch('utils.get_spot_capital_per_position', return_value=16.0), \
             patch('capital_manager.validate_spot_order', return_value=(True, 'OK', 16.0)), \
             patch('decision_timeline.record_order_event'), \
             patch('decision_timeline.record_protection_event'):
            pos, message = longs.open_long(candidate, {'positions': []}, max_longs=1)
        return client, pos, message

    def run_cycle(self, client, state, *, allow_manage=False):
        saved = []
        analytics = Mock()
        runner = cycle_runner.CycleRunner(
            out_fn=Mock(), analytics=analytics, binance=client,
            safe_log_open_fn=Mock(), safe_log_close_fn=Mock(),
            safe_log_decision_snapshot_fn=Mock(),
            safe_persist_bot_state_fn=Mock(), audit_orphans_fn=Mock(),
            maybe_clean_dust_fn=Mock(), check_partial_long_fn=Mock(),
            check_partial_short_fn=Mock(), handle_close_fn=Mock(),
        )
        state['status'] = 'active'

        def market_close(_context):
            state['status'] = 'paused'
            return False, False, 'offline stop after lifecycle'

        with patch('utils.load_state', return_value=state), \
             patch('utils.save_state', side_effect=lambda value: saved.append(json.loads(json.dumps(value)))), \
             patch.object(cycle_runner.market, 'get_btc_context', return_value={
                 'trend': 'neutral', 'btc_price': 110000, 'change_4h': 0,
             }), \
             patch.object(cycle_runner.market, 'check_btc_momentum_close', side_effect=market_close), \
             patch.object(cycle_runner.rebalance, 'rebalance', return_value=(False, '')), \
             patch.object(cycle_runner, 'sync_preventive_telegram_alert'), \
             patch.object(cycle_runner.operational_state, 'transition'), \
             patch('decision_timeline.record_cycle_start'), \
             patch('decision_timeline.record_event'), \
             patch('utils.send_alert'), \
             patch.object(longs, 'manage_long',
                          side_effect=None if allow_manage else AssertionError('manage_long must defer'),
                          return_value=('hold', 110000, 0)) as manage, \
             patch.object(cycle_runner.partial_spot_long, 'reconcile_pending_partial_long_spot',
                          side_effect=AssertionError('entry must not enter partial recovery')):
            runner.run()
        return saved, analytics, runner, manage

    def test_e1_e2_e3_e7_e10_e16_rejected_exit_protects_only_managed(self):
        client, pos, _ = self.open_entry()
        self.assertTrue(is_spot_long_recovery_pending(pos))
        self.assertEqual(spot_long_recovery_kind(pos), 'ENTRY_PROTECTION')
        self.assertEqual(len(client.sell_calls), 1)
        self.assertEqual(len(client.oco_calls), 1)
        result = entry_spot_recovery.reconcile_pending_entry_spot_long(client, pos)
        self.assertEqual(result['status'], 'ENTRY_PROTECTED')
        self.assertFalse(pos['recovery_pending'])
        self.assertEqual(pos['quantity'], 0.00016)
        self.assertEqual(client.oco_calls[-1]['quantity'], '0.00016')
        self.assertNotEqual(client.oco_calls[-1]['quantity'], '0.00018783')
        self.assertEqual(len(client.sell_calls), 1)

    def test_e4_timeout_is_explicit_exit_ambiguity_and_no_blind_oco(self):
        client, pos, _ = self.open_entry(sell_mode='timeout_unknown')
        self.assertEqual(spot_long_recovery_kind(pos), 'ENTRY_EMERGENCY_EXIT')
        self.assertTrue(pos['recovery_pending'])
        self.assertEqual(len(client.sell_calls), 1)
        self.assertEqual(len(client.oco_calls), 1)

    def test_e7_canceled_zero_exit_can_restore_only_managed(self):
        client, pos, _ = self.open_entry(sell_mode='timeout_unknown')
        client.set_sell('CANCELED')
        result = entry_spot_recovery.reconcile_pending_entry_spot_long(client, pos)
        self.assertEqual(result['status'], 'ENTRY_PROTECTED')
        self.assertEqual(client.oco_calls[-1]['quantity'], '0.00016')
        self.assertEqual(len(client.sell_calls), 1)

    def test_entry_buy_without_confirmed_fill_cannot_authorize_sell(self):
        client, pos, _ = self.open_entry()
        other = dict(pos)
        other['entry_spot_recovery'] = None
        result = entry_spot_recovery.prepare_entry_recovery(
            client, other, {'symbol': 'BTCUSDT', 'side': 'BUY', 'status': 'NEW',
                            'orderId': 101, 'executedQty': '0.00016'}, '0.00016',
        )
        self.assertEqual(result['status'], 'ENTRY_QUANTITY_NOT_OPERABLE')
        self.assertFalse(other['entry_spot_recovery']['sell_attempted'])

    def test_e5_timeout_then_filled_never_reprotects_sold_quantity(self):
        client, pos, _ = self.open_entry(sell_mode='timeout_unknown')
        client.set_sell('FILLED', '0.00016')
        result = entry_spot_recovery.reconcile_pending_entry_spot_long(client, pos)
        self.assertEqual(result['status'], 'ENTRY_EXIT_CONFIRMED_MANUAL_FINALIZATION')
        self.assertTrue(result['confirmed_flat'])
        self.assertEqual(pos['quantity'], 0)
        self.assertTrue(pos['recovery_pending'])
        self.assertEqual(len(client.oco_calls), 1)

    def test_e6_partial_fill_keeps_exact_residual_and_lock(self):
        client, pos, _ = self.open_entry(sell_mode='timeout_unknown')
        client.set_sell('PARTIALLY_FILLED', '0.00003')
        result = entry_spot_recovery.reconcile_pending_entry_spot_long(client, pos)
        self.assertEqual(result['status'], 'ENTRY_EXIT_PARTIALLY_FILLED')
        self.assertEqual(pos['quantity'], 0.00013)
        self.assertTrue(pos['recovery_pending'])
        self.assertEqual(len(client.sell_calls), 1)

    def test_e8_get_failure_keeps_lock(self):
        client, pos, _ = self.open_entry()
        client.query_error = True
        result = entry_spot_recovery.reconcile_pending_entry_spot_long(client, pos)
        self.assertEqual(result['status'], 'ENTRY_REQUERY_FAILED')
        self.assertTrue(pos['recovery_pending'])

    def test_e9_e19_missing_or_conflicting_evidence_is_unknown(self):
        client, pos, _ = self.open_entry()
        pos.pop('entry_spot_recovery')
        self.assertEqual(spot_long_recovery_kind(pos), 'UNKNOWN')
        self.assertEqual(len(client.oco_calls), 1)
        pos['entry_spot_recovery'] = {'kind': 'entry_protection_v1'}
        result = entry_spot_recovery.reconcile_pending_entry_spot_long(client, pos)
        self.assertEqual(result['status'], 'ENTRY_RECOVERY_EVIDENCE_INVALID')
        self.assertTrue(pos['recovery_pending'])

    def test_e11_e12_e13_partial_and_entry_reconcilers_do_not_cross(self):
        client, pos, _ = self.open_entry(sell_mode='timeout_unknown')
        self.assertEqual(spot_long_recovery_kind(pos), 'ENTRY_EMERGENCY_EXIT')
        self.assertEqual(partial_spot_long.reconcile_pending_partial_long_spot(client, pos)['status'],
                         'RECOVERY_EVIDENCE_MISSING')
        pos['partial_spot_recovery'] = {'kind': 'partial_long_spot_v1'}
        self.assertEqual(spot_long_recovery_kind(pos), 'UNKNOWN')
        self.assertTrue(pos['recovery_pending'])

    def test_e14_e15_guardian_and_preventive_defer_entry_ambiguity(self):
        client, pos, _ = self.open_entry(sell_mode='timeout_unknown')
        with patch('decision_timeline.record_event'):
            result = preventive_spot_close.attempt_preventive_long_spot_close(client, pos)
        self.assertEqual(result['status'], 'DEFERRED_RECOVERY_PENDING')
        state = {'positions': [pos]}
        with patch.object(sl_guardian, 'BINANCE', client), \
             patch('utils.load_state', return_value=state), \
             patch('utils.save_state') as save_state, \
             patch.object(sl_guardian, '_close_spot_market') as close, \
             patch('decision_timeline.record_guardian_event'):
            sl_guardian._run()
        close.assert_not_called()
        save_state.assert_not_called()
        self.assertEqual(state['positions'], [pos])

    def test_e17_failed_oco_recovery_keeps_lock(self):
        client, pos, _ = self.open_entry()
        client.restore_error = True
        result = entry_spot_recovery.reconcile_pending_entry_spot_long(client, pos)
        self.assertEqual(result['status'], 'ENTRY_OCO_CREATE_UNCONFIRMED')
        self.assertTrue(pos['recovery_pending'])

    def test_e20_e21_restart_preserves_evidence_and_reconciles(self):
        client, pos, _ = self.open_entry()
        restored = json.loads(json.dumps(pos))
        self.assertEqual(restored['entry_spot_recovery']['kind'], 'entry_protection_v1')
        self.assertEqual(restored['entry_spot_recovery']['emergency_quantity'], '0.00016')
        result = entry_spot_recovery.reconcile_pending_entry_spot_long(client, restored)
        self.assertEqual(result['status'], 'ENTRY_PROTECTED')

    def test_e22_e23_no_invented_pnl_or_premature_removal(self):
        client, pos, _ = self.open_entry(sell_mode='timeout_unknown')
        state = {'positions': [pos], 'total_pnl_usdt': 0.0, 'daily_pnl_usdt': 0.0}
        result = entry_spot_recovery.reconcile_pending_entry_spot_long(client, pos)
        self.assertEqual(result['status'], 'ENTRY_REQUERY_FAILED')
        self.assertEqual(state['positions'], [pos])
        self.assertEqual(state['total_pnl_usdt'], 0)
        self.assertNotIn('partial_pnl', pos)

    def test_e24_only_offline_client_used(self):
        client, pos, _ = self.open_entry()
        entry_spot_recovery.reconcile_pending_entry_spot_long(client, pos)
        self.assertEqual(len(client.sell_calls), 1)

    def test_e18_e25_cycle_persists_lock_then_resumes_only_after_protection(self):
        client, pos, _ = self.open_entry()
        state = {
            'positions': [pos], 'pnl_date': time.strftime('%Y-%m-%d', time.gmtime()),
            'last_bl_review': time.time(), 'total_pnl_usdt': 0.0, 'daily_pnl_usdt': 0.0,
        }
        saved, analytics, runner, manage = self.run_cycle(client, state)
        self.assertFalse(pos['recovery_pending'])
        self.assertEqual(saved[-1]['positions'][0]['quantity'], 0.00016)
        self.assertFalse(saved[-1]['positions'][0]['recovery_pending'])
        self.assertEqual(len(client.sell_calls), 1)
        self.assertEqual(len(client.oco_calls), 2)
        manage.assert_not_called()
        analytics.log_trade_close.assert_not_called()
        runner.handle_close.assert_not_called()
        _, _, _, manage = self.run_cycle(client, state, allow_manage=True)
        manage.assert_called_once()

    def test_e18_e25_ambiguous_entry_stays_locked_across_cycles(self):
        client, pos, _ = self.open_entry(sell_mode='timeout_unknown')
        state = {
            'positions': [pos], 'pnl_date': time.strftime('%Y-%m-%d', time.gmtime()),
            'last_bl_review': time.time(), 'total_pnl_usdt': 0.0, 'daily_pnl_usdt': 0.0,
        }
        for _ in range(2):
            saved, analytics, runner, manage = self.run_cycle(client, state)
            self.assertTrue(saved[-1]['positions'][0]['recovery_pending'])
            self.assertEqual(saved[-1]['positions'][0]['entry_spot_recovery']['emergency_client_order_id'],
                             client.sell_calls[0]['newClientOrderId'])
            manage.assert_not_called()
            analytics.log_trade_close.assert_not_called()
            runner.handle_close.assert_not_called()
        self.assertEqual(len(client.sell_calls), 1)
        self.assertEqual(len(client.oco_calls), 1)
        self.assertEqual(state['positions'], [pos])


if __name__ == '__main__':
    unittest.main()
