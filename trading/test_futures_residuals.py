#!/usr/bin/env python3
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(__file__))

import futures_residuals


class FuturesResidualTests(unittest.TestCase):
    def _client(self, symbol='SPCXUSDT', before_amt='-0.01', before_notional='-1.46',
                after_amt='0', after_notional='0', open_orders=None):
        client = Mock()
        client.futures_position_risk.side_effect = [
            [{'symbol': symbol, 'positionAmt': before_amt, 'notional': before_notional, 'entryPrice': '146', 'markPrice': '146'}],
            [{'symbol': symbol, 'positionAmt': after_amt, 'notional': after_notional, 'entryPrice': '146', 'markPrice': '146'}],
        ]
        client.futures_open_orders.return_value = [] if open_orders is None else open_orders
        client.get_futures_filters.return_value = {'step_size': 0.01, 'min_qty': 0.01}
        client.create_futures_order.return_value = {'orderId': 991}
        return client

    def test_partial_small_residual_without_orders_closes_reduce_only(self):
        state = {'positions': [{'symbol': 'SPCXUSDT', 'direction': 'short', 'quantity': 0.12}]}
        pos = state['positions'][0]
        client = self._client()

        with tempfile.TemporaryDirectory() as tmp:
            result = futures_residuals.handle_after_partial_short(
                pos,
                state,
                client,
                out_fn=Mock(),
                alert_fn=Mock(),
                max_notional=3.0,
                report_dir=tmp,
            )

            reports = os.listdir(tmp)

        self.assertTrue(result['ok'])
        self.assertEqual(result['status'], 'closed')
        client.create_futures_order.assert_called_once_with({
            'symbol': 'SPCXUSDT',
            'side': 'BUY',
            'type': 'MARKET',
            'quantity': '0.01',
            'reduceOnly': 'true',
        })
        self.assertEqual(state['positions'], [])
        self.assertEqual(len(reports), 1)

    def test_large_residual_without_orders_attempts_recreate_protection(self):
        state = {'positions': [{'symbol': 'SPCXUSDT', 'direction': 'short', 'quantity': 0.12, 'tp': 130, 'sl': 150}]}
        pos = state['positions'][0]
        client = self._client(before_amt='-0.12', before_notional='-17.52', open_orders=[])
        client.futures_open_orders.side_effect = [[], [{'orderId': 111}]]

        result = futures_residuals.handle_after_partial_short(
            pos,
            state,
            client,
            out_fn=Mock(),
            alert_fn=Mock(),
            max_notional=3.0,
        )

        self.assertTrue(result['ok'])
        self.assertEqual(result['status'], 'protection_recreated')
        payloads = [call.args[0] for call in client.create_futures_order.call_args_list]
        self.assertIn('LIMIT', {payload['type'] for payload in payloads})
        self.assertIn('STOP_MARKET', {payload['type'] for payload in payloads})

    def test_large_residual_without_orders_blocks_if_protection_cannot_be_recreated(self):
        state = {'positions': [{'symbol': 'SPCXUSDT', 'direction': 'short', 'quantity': 0.12, 'tp': 130, 'sl': 150}]}
        pos = state['positions'][0]
        client = self._client(before_amt='-0.12', before_notional='-17.52', open_orders=[])
        client.futures_open_orders.side_effect = [[], []]
        alert = Mock()

        result = futures_residuals.handle_after_partial_short(
            pos,
            state,
            client,
            out_fn=Mock(),
            alert_fn=alert,
            max_notional=3.0,
        )

        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'unprotected_large_position')
        self.assertTrue(state['futures_entries_blocked'])
        self.assertEqual(state['futures_entries_block_reason'], 'Futures unprotected position present')
        alert.assert_called_once()

    def test_unprotected_risk_blocks_new_futures_entries(self):
        status = {'summary': {'unprotected_count': 1}}

        blocked, reason = futures_residuals.has_unprotected_futures_risk(status)

        self.assertTrue(blocked)
        self.assertEqual(reason, 'Futures unprotected position present')

    def test_config_can_disable_unprotected_entry_block(self):
        status = {'summary': {'unprotected_count': 1}}

        with patch('futures_residuals.config.FUTURES_UNPROTECTED_BLOCK_NEW_ENTRIES', False):
            blocked, reason = futures_residuals.has_unprotected_futures_risk(status)

        self.assertFalse(blocked)
        self.assertIsNone(reason)


if __name__ == '__main__':
    unittest.main()
