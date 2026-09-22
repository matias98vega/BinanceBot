#!/usr/bin/env python3
"""Offline lifecycle integration for unresolved Spot LONG partial execution."""

import json
import os
import sys
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(__file__))

import longs
import partial_spot_long
import preventive_spot_close
import sl_guardian
from orchestration import audit_pipeline, cycle_runner, position_lifecycle
from spot_recovery_lock import is_spot_long_recovery_pending
from test_partial_spot_long import OfflinePartialClient, _position


class CycleRecoveryLockTests(unittest.TestCase):
    def _runner(self, client, state):
        analytics = Mock()
        safe_persist = Mock()
        saved = []

        def check_partial(pos, current):
            position_lifecycle.check_partial_long(
                pos, current, client, Mock(), analytics, Mock(),
            )

        runner = cycle_runner.CycleRunner(
            out_fn=Mock(), analytics=analytics, binance=client,
            safe_log_open_fn=Mock(), safe_log_close_fn=Mock(),
            safe_log_decision_snapshot_fn=Mock(),
            safe_persist_bot_state_fn=safe_persist,
            audit_orphans_fn=Mock(), maybe_clean_dust_fn=Mock(),
            check_partial_long_fn=check_partial,
            check_partial_short_fn=Mock(), handle_close_fn=Mock(),
        )
        return runner, analytics, saved

    def _run_cycle(self, runner, state, saved, close_longs=False, allow_manage=False):
        state['status'] = 'active'

        def market_close(_context):
            # Stop after position lifecycle, preserving the real cycle path and persistence.
            state['status'] = 'paused'
            return False, close_longs, 'offline preventive condition'

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
             patch('utils.format_trade_close_alert', return_value='offline alert'), \
             patch.object(longs, 'manage_long',
                          side_effect=None if allow_manage else AssertionError('manage_long must be deferred'),
                          return_value=('hold', 110000, 0)) as manage:
            runner.run()
        return manage

    def test_r1_r5_r6_r22_r23_r24_r25_timeout_defers_same_and_next_cycle(self):
        client = OfflinePartialClient()
        client.sell_mode = 'timeout_unknown'
        pos = _position()
        state = {
            'status': 'active', 'positions': [pos], 'pnl_date': time.strftime('%Y-%m-%d', time.gmtime()),
            'last_bl_review': time.time(), 'total_pnl_usdt': 0.0, 'daily_pnl_usdt': 0.0,
        }
        runner, analytics, saved = self._runner(client, state)
        self._run_cycle(runner, state, saved)
        self.assertTrue(is_spot_long_recovery_pending(pos))
        self.assertEqual(len(client.order_calls), 1)
        self.assertEqual(len(client.cancel_calls), 1)
        self.assertEqual(len(state['positions']), 1)
        self.assertEqual(saved[-1]['positions'][0]['partial_spot_recovery']['attempted_quantity'], '0.00008')

        self._run_cycle(runner, state, saved, close_longs=True)
        self.assertTrue(is_spot_long_recovery_pending(pos))
        self.assertEqual(len(client.order_calls), 1)
        self.assertEqual(len(client.oco_calls), 0)
        self.assertEqual(len(state['positions']), 1)
        self.assertEqual(state['total_pnl_usdt'], 0.0)
        analytics.log_trade_close.assert_not_called()
        runner.safe_log_close.assert_not_called()
        runner.handle_close.assert_not_called()

    def test_r7_filled_requery_unlocks_only_after_protection_and_next_cycle_resumes(self):
        client = OfflinePartialClient()
        client.sell_mode = 'timeout_unknown'
        pos = _position()
        state = {
            'status': 'active', 'positions': [pos], 'pnl_date': time.strftime('%Y-%m-%d', time.gmtime()),
            'last_bl_review': time.time(), 'total_pnl_usdt': 0.0, 'daily_pnl_usdt': 0.0,
        }
        runner, analytics, saved = self._runner(client, state)
        self._run_cycle(runner, state, saved)
        self.assertTrue(pos['recovery_pending'])

        client._fill(client.order_calls[0])
        self._run_cycle(runner, state, saved)
        self.assertFalse(pos['recovery_pending'])
        self.assertTrue(pos['partial_taken'])
        self.assertEqual(pos['quantity'], 0.00008)
        self.assertEqual(client.oco_calls[-1]['quantity'], '0.00008')
        self.assertEqual(len(client.order_calls), 1)
        analytics.log_trade_close.assert_called_once()
        self.assertEqual(len(state['positions']), 1)

        manage = self._run_cycle(runner, state, saved, allow_manage=True)
        manage.assert_called_once()
        analytics.log_trade_close.assert_called_once()

    def test_r2_manage_long_and_normal_recovery_are_defensive(self):
        pos = _position()
        pos['recovery_pending'] = True
        with patch.object(longs, '_recolocar_oco', side_effect=AssertionError('normal OCO recovery')):
            self.assertEqual(longs.manage_long(pos, {'positions': [pos]})[0], 'deferred_recovery_pending')
        self.assertEqual(longs._recolocar_oco(pos, {'positions': [pos]})[0], 'deferred_recovery_pending')

    def test_r3_r14_preventive_condition_defers_without_exchange_access(self):
        pos = _position()
        pos['recovery_pending'] = True
        client = Mock()
        with patch('decision_timeline.record_event') as event:
            result = preventive_spot_close.attempt_preventive_long_spot_close(client, pos)
        self.assertEqual(result['status'], 'DEFERRED_RECOVERY_PENDING')
        self.assertFalse(result['confirmed_close'])
        client.assert_not_called()
        event.assert_called_once()
        self.assertTrue(pos['recovery_pending'])

    def test_r4_r15_guardian_sl_condition_does_not_sell_or_remove(self):
        pos = _position()
        pos.update(recovery_pending=True, oco_order_list_id='', sl=120000.0)
        state = {'positions': [pos]}
        client = OfflinePartialClient()
        with patch.object(sl_guardian, 'BINANCE', client), \
             patch('utils.load_state', return_value=state), \
             patch('utils.save_state') as save_state, \
             patch.object(sl_guardian, '_close_spot_market') as close, \
             patch('decision_timeline.record_guardian_event') as event:
            sl_guardian._run()
        close.assert_not_called()
        save_state.assert_not_called()
        self.assertEqual(state['positions'], [pos])
        self.assertIn('guardian_recovery_deferred', [call.args[0] for call in event.call_args_list])

    def test_reconciliation_audit_cannot_remove_locked_position(self):
        pos = _position()
        pos['recovery_pending'] = True
        state = {'positions': [pos]}
        client = Mock()
        result = audit_pipeline.reconcile_stale_spot_positions(
            state, client, save_state_fn=Mock(),
        )
        self.assertEqual(result[0]['classification'], 'DEFERRED_RECOVERY_PENDING')
        self.assertEqual(state['positions'], [pos])
        client.assert_not_called()

    def test_r16_normal_manage_long_remains_available_without_lock(self):
        pos = _position()
        pos['oco_order_list_id'] = ''
        with patch.object(longs, '_recolocar_oco', return_value=('updated', 110000, 0)) as recover:
            result = longs.manage_long(pos, {'positions': [pos]})
        self.assertEqual(result[0], 'updated')
        recover.assert_called_once()

    def test_r17_normal_partial_success_remains_available(self):
        client, pos = OfflinePartialClient(), _position()
        result = partial_spot_long.attempt_partial_long_spot(client, pos, 110000)
        self.assertTrue(result['confirmed_execution'])
        self.assertFalse(pos['recovery_pending'])
        self.assertEqual(client.order_calls[0]['quantity'], '0.00008')


if __name__ == '__main__':
    unittest.main()
