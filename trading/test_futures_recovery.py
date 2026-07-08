#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(__file__))

import futures_recovery


class BinanceHTTPError(Exception):
    code = 400
    status = 400
    reason = 'Bad Request'
    binance_body = '{"code":-2019,"msg":"Margin is insufficient."}'


class FuturesRecoveryTests(unittest.TestCase):
    def _status_file(self, tmp, symbol='NEARUSDT', amount=-12, managed=False, classes=None):
        path = os.path.join(tmp, 'futures_reconciliation_status.json')
        classes = classes or [
            'observed_futures_position',
            'unmanaged_futures_position',
            'orphan_futures_position',
            'unprotected_futures_position',
            'desynced_closed_but_open_on_exchange',
        ]
        payload = {
            'summary': {'observed_count': 1},
            'positions': {
                symbol: {
                    'symbol': symbol,
                    'side': 'SHORT' if amount < 0 else 'LONG',
                    'position_amt': amount,
                    'notional': abs(amount) * 2,
                    'unrealized_pnl': -1.2,
                    'managed_in_state': managed,
                    'classification': classes,
                    'open_orders_count': 0,
                }
            },
        }
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(payload, f)
        return path

    def _client(self, symbol='NEARUSDT', before='-12', after='0', filters=None):
        client = Mock()
        client.futures_position_risk.side_effect = [
            [{'symbol': symbol, 'positionAmt': before, 'notional': '-24', 'entryPrice': '2', 'markPrice': '2.1'}],
            [{'symbol': symbol, 'positionAmt': after, 'notional': '0', 'entryPrice': '2', 'markPrice': '2.1'}],
        ]
        client.get_futures_filters.return_value = filters or {'step_size': 0.1, 'min_qty': 0.1}
        client.create_futures_order.return_value = {'orderId': 123}
        return client

    def test_preview_does_not_send_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = self._status_file(tmp)
            preview = futures_recovery.preview_recovery(status)

        self.assertEqual(len(preview['candidates']), 1)
        self.assertEqual(preview['candidates'][0]['order']['side'], 'BUY')

    def test_close_without_confirm_does_not_send_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = self._status_file(tmp)
            client = self._client()

            result = futures_recovery.close_position('NEARUSDT', confirm=None, client=client, status_file=status)

        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'missing_confirm')
        client.create_futures_order.assert_not_called()
        client.futures_position_risk.assert_not_called()

    def test_short_sends_buy_market_reduce_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = self._status_file(tmp, amount=-12)
            client = self._client(before='-12', after='0')

            result = futures_recovery.close_position('NEARUSDT', confirm='CONFIRM', client=client, status_file=status)

        self.assertTrue(result['ok'])
        client.create_futures_order.assert_called_once_with({
            'symbol': 'NEARUSDT',
            'side': 'BUY',
            'type': 'MARKET',
            'quantity': '12.0',
            'reduceOnly': 'true',
        })

    def test_long_sends_sell_market_reduce_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = self._status_file(tmp, symbol='BTCUSDT', amount=0.5)
            client = self._client(symbol='BTCUSDT', before='0.5', after='0', filters={'step_size': 0.001, 'min_qty': 0.001})

            result = futures_recovery.close_position('BTCUSDT', confirm='CONFIRM', client=client, status_file=status)

        self.assertTrue(result['ok'])
        payload = client.create_futures_order.call_args.args[0]
        self.assertEqual(payload['side'], 'SELL')
        self.assertEqual(payload['type'], 'MARKET')
        self.assertEqual(payload['reduceOnly'], 'true')

    def test_managed_position_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = self._status_file(tmp, managed=True)
            client = self._client()

            result = futures_recovery.close_position('NEARUSDT', confirm='CONFIRM', client=client, status_file=status)

        self.assertEqual(result['reason'], 'managed_position')
        client.create_futures_order.assert_not_called()

    def test_symbol_not_reconciled_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = self._status_file(tmp)
            client = self._client()

            result = futures_recovery.close_position('ADAUSDT', confirm='CONFIRM', client=client, status_file=status)

        self.assertEqual(result['reason'], 'symbol_not_reconciled')
        client.create_futures_order.assert_not_called()

    def test_non_candidate_classification_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = self._status_file(tmp, classes=['observed_futures_position'])
            client = self._client()

            result = futures_recovery.close_position('NEARUSDT', confirm='CONFIRM', client=client, status_file=status)

        self.assertEqual(result['reason'], 'not_recovery_candidate')
        client.create_futures_order.assert_not_called()

    def test_reconsults_binance_before_and_after_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = self._status_file(tmp)
            client = self._client()

            futures_recovery.close_position('NEARUSDT', confirm='CONFIRM', client=client, status_file=status)

        self.assertEqual(client.futures_position_risk.call_count, 2)

    def test_min_qty_blocks_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = self._status_file(tmp, amount=-0.01)
            client = self._client(before='-0.01', filters={'step_size': 0.1, 'min_qty': 0.1})

            result = futures_recovery.close_position('NEARUSDT', confirm='CONFIRM', client=client, status_file=status)

        self.assertEqual(result['reason'], 'unclosable_by_min_qty')
        client.create_futures_order.assert_not_called()

    def test_binance_error_preserves_code_msg_raw_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = self._status_file(tmp)
            client = self._client()
            client.create_futures_order.side_effect = BinanceHTTPError('HTTP 400')

            result = futures_recovery.close_position('NEARUSDT', confirm='CONFIRM', client=client, status_file=status)

        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], -2019)
        self.assertEqual(result['msg'], 'Margin is insufficient.')
        self.assertIn('Margin is insufficient', result['raw_body'])

    def test_format_preview_mentions_confirm_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = self._status_file(tmp)
            text = futures_recovery.format_preview_text(futures_recovery.preview_recovery(status))

        self.assertIn('/futures_recovery_close NEARUSDT CONFIRM', text)

    def test_close_managed_residual_requires_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = self._status_file(tmp, amount=-0.01, managed=True, classes=[
                'observed_futures_position',
                'managed_futures_position',
                'unprotected_futures_position',
            ])
            client = self._client(before='-0.01', after='0', filters={'step_size': 0.01, 'min_qty': 0.01})

            result = futures_recovery.close_managed_residual('NEARUSDT', confirm=None, client=client, status_file=status)

        self.assertEqual(result['reason'], 'missing_confirm')
        client.create_futures_order.assert_not_called()

    def test_close_managed_residual_rejects_notional_above_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = self._status_file(tmp, amount=-12, managed=True, classes=[
                'observed_futures_position',
                'managed_futures_position',
                'unprotected_futures_position',
            ])
            client = self._client(before='-12', after='0')

            result = futures_recovery.close_managed_residual(
                'NEARUSDT',
                confirm='CONFIRM',
                client=client,
                status_file=status,
                max_notional=3.0,
            )

        self.assertEqual(result['reason'], 'notional_above_threshold')
        client.create_futures_order.assert_not_called()

    def test_close_managed_residual_sends_reduce_only_and_cleans_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = self._status_file(tmp, amount=-0.01, managed=True, classes=[
                'observed_futures_position',
                'managed_futures_position',
                'unprotected_futures_position',
            ])
            state = {'positions': [{'symbol': 'NEARUSDT', 'direction': 'short', 'quantity': 0.12}]}
            client = self._client(before='-0.01', after='0', filters={'step_size': 0.01, 'min_qty': 0.01})

            with patch('futures_recovery.utils.save_state') as save_state:
                result = futures_recovery.close_managed_residual(
                    'NEARUSDT',
                    confirm='CONFIRM',
                    client=client,
                    status_file=status,
                    max_notional=3.0,
                    state=state,
                    report_dir=tmp,
                )

        self.assertTrue(result['ok'])
        client.create_futures_order.assert_called_once_with({
            'symbol': 'NEARUSDT',
            'side': 'BUY',
            'type': 'MARKET',
            'quantity': '0.01',
            'reduceOnly': 'true',
        })
        self.assertEqual(state['positions'], [])
        save_state.assert_called_once_with(state)


if __name__ == '__main__':
    unittest.main()
