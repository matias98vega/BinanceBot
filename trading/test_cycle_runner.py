#!/usr/bin/env python3
import os
import sys
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('BINANCE_API_KEY', 'test')
os.environ.setdefault('BINANCE_API_SECRET', 'test')

sys.path.insert(0, os.path.dirname(__file__))

from orchestration import cycle_runner


class CycleRunnerTests(unittest.TestCase):
    def test_paused_cycle_runs_audit_and_persists_warning(self):
        state = {
            'status': 'paused',
            'daily_pnl_usdt': 0.0,
            'positions': [],
            'pnl_date': time.strftime('%Y-%m-%d', time.gmtime()),
            'last_bl_review': time.time(),
        }
        audit_orphans = Mock()
        safe_persist = Mock()
        runner = cycle_runner.CycleRunner(
            out_fn=Mock(),
            analytics=Mock(),
            binance=Mock(),
            safe_log_open_fn=Mock(),
            safe_log_close_fn=Mock(),
            safe_log_decision_snapshot_fn=Mock(),
            safe_persist_bot_state_fn=safe_persist,
            audit_orphans_fn=audit_orphans,
            maybe_clean_dust_fn=Mock(),
            check_partial_long_fn=Mock(),
            check_partial_short_fn=Mock(),
            handle_close_fn=Mock(),
        )

        with patch('utils.load_state', return_value=state), \
             patch('utils.save_state') as save_state, \
             patch('decision_timeline.record_cycle_start'):
            runner.run()

        audit_orphans.assert_called_once_with(state)
        safe_persist.assert_called_once_with(state, system_health='WARNING')
        save_state.assert_called_once_with(state)


if __name__ == '__main__':
    unittest.main()
