#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(__file__))

import futures_reconciliation


class FuturesReconciliationTests(unittest.TestCase):
    def _write_trades(self, path, records):
        with open(path, 'w', encoding='utf-8') as f:
            for record in records:
                f.write(json.dumps(record) + '\n')

    def _observed_short(self, symbol='CRCLUSDT'):
        return {
            'symbol': symbol,
            'side': 'SHORT',
            'position_amt': -0.17,
            'notional': 11.67,
            'entry_price': 63.53,
            'mark_price': 68.63,
            'unrealized_pnl': -0.87,
            'leverage': 2,
            'margin_type': 'cross',
            'position_margin': 5.8,
        }

    def _raw_binance_short(self, symbol='CRCLUSDT'):
        return {
            'symbol': symbol,
            'positionAmt': '-0.17',
            'notional': '-11.67',
            'entryPrice': '63.53',
            'markPrice': '68.63',
            'unRealizedProfit': '-0.87',
            'leverage': '2',
            'marginType': 'cross',
            'isolatedMargin': '0',
            'liquidationPrice': '120.50',
        }

    def _managed_state_short(self, symbol='CRCLUSDT', trade_id='short_CRCLUSDT_1783540416', quantity=0.49, tp_order_id='634772815'):
        return {
            'id': trade_id,
            'symbol': symbol,
            'direction': 'short',
            'entry_price': 64.2,
            'quantity': quantity,
            'tp': 62.24,
            'tp_order_id': tp_order_id,
            'sl_order_id': '',
            'partial_taken': False,
            'entry_time': '2026-07-08T12:00:00Z',
        }

    def _reduce_only_tp_order(self, symbol='CRCLUSDT', order_id='634772815', quantity='0.49', price='62.24'):
        return {
            'symbol': symbol,
            'orderId': int(order_id),
            'side': 'BUY',
            'type': 'LIMIT',
            'reduceOnly': True,
            'origQty': quantity,
            'price': price,
        }

    def test_raw_binance_position_amt_detects_open_short(self):
        with tempfile.TemporaryDirectory() as tmp:
            trades = os.path.join(tmp, 'trades.jsonl')
            self._write_trades(trades, [])

            positions = futures_reconciliation.classify_positions(
                [self._raw_binance_short()],
                state={'positions': []},
                open_orders_by_symbol={'CRCLUSDT': []},
                trades_file=trades,
            )

        self.assertIn('CRCLUSDT', positions)
        self.assertEqual(positions['CRCLUSDT']['position_amt'], -0.17)
        self.assertEqual(positions['CRCLUSDT']['notional'], 11.67)
        self.assertEqual(positions['CRCLUSDT']['liquidation_price'], 120.50)

    def test_normalized_snapshot_quantity_detects_open_short(self):
        with tempfile.TemporaryDirectory() as tmp:
            trades = os.path.join(tmp, 'trades.jsonl')
            self._write_trades(trades, [])

            positions = futures_reconciliation.classify_positions(
                [{'symbol': 'SUIUSDT', 'side': 'SHORT', 'quantity': 0.1, 'notional': 0.075}],
                state={'positions': []},
                open_orders_by_symbol={'SUIUSDT': []},
                trades_file=trades,
            )

        self.assertIn('SUIUSDT', positions)
        self.assertEqual(positions['SUIUSDT']['position_amt'], -0.1)
        self.assertEqual(positions['SUIUSDT']['notional'], 0.075)

    def test_observed_short_without_open_orders_is_unprotected(self):
        with tempfile.TemporaryDirectory() as tmp:
            trades = os.path.join(tmp, 'trades.jsonl')
            self._write_trades(trades, [])

            positions = futures_reconciliation.classify_positions(
                [self._observed_short()],
                state={'positions': [{'symbol': 'CRCLUSDT', 'direction': 'short', 'entry_price': 63, 'quantity': 0.17, 'entry_time': 1}]},
                open_orders_by_symbol={'CRCLUSDT': []},
                trades_file=trades,
            )

        classes = positions['CRCLUSDT']['classification']
        self.assertIn('observed_futures_position', classes)
        self.assertIn('managed_futures_position', classes)
        self.assertIn('unprotected_futures_position', classes)

    def test_persist_status_not_empty_when_observed_positions_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            trades = os.path.join(tmp, 'trades.jsonl')
            status = os.path.join(tmp, 'futures_reconciliation_status.json')
            self._write_trades(trades, [])

            payload = futures_reconciliation.reconcile_observed_positions(
                [self._raw_binance_short('CRCLUSDT'), self._raw_binance_short('BNBUSDT')],
                state={'positions': []},
                open_orders_by_symbol={'CRCLUSDT': [], 'BNBUSDT': []},
                trades_file=trades,
                status_file=status,
                allowed_count=0,
            )

            saved = futures_reconciliation.load_status(status)

        self.assertEqual(payload['summary']['observed_count'], 2)
        self.assertEqual(saved['summary']['observed_count'], 2)
        self.assertEqual(saved['summary']['unprotected_count'], 2)
        self.assertEqual(saved['summary']['status'], 'EXCESO FUTURES / RIESGO NO GESTIONADAS')
        self.assertIn('CRCLUSDT', saved['positions'])

    def test_observed_short_missing_from_state_is_unmanaged_orphan(self):
        with tempfile.TemporaryDirectory() as tmp:
            trades = os.path.join(tmp, 'trades.jsonl')
            self._write_trades(trades, [])

            positions = futures_reconciliation.classify_positions(
                [self._observed_short('NEARUSDT')],
                state={'positions': []},
                open_orders_by_symbol={'NEARUSDT': []},
                trades_file=trades,
            )

        classes = positions['NEARUSDT']['classification']
        self.assertIn('unmanaged_futures_position', classes)
        self.assertIn('orphan_futures_position', classes)

    def test_closed_history_but_open_exchange_is_desynced(self):
        with tempfile.TemporaryDirectory() as tmp:
            trades = os.path.join(tmp, 'trades.jsonl')
            self._write_trades(trades, [
                {'event_type': 'TRADE_OPEN', 'trade_id': 'short_CRCLUSDT_1', 'symbol': 'CRCLUSDT', 'side': 'SHORT', 'status': 'OPEN'},
                {'event_type': 'TRADE_CLOSE', 'trade_id': 'short_CRCLUSDT_1', 'symbol': 'CRCLUSDT', 'side': 'SHORT', 'status': 'CLOSED', 'exit_reason': 'PREVENTIVE_BTC_MOMENTUM'},
            ])

            positions = futures_reconciliation.classify_positions(
                [self._observed_short()],
                state={'positions': []},
                open_orders_by_symbol={'CRCLUSDT': []},
                trades_file=trades,
            )

        self.assertIn('desynced_closed_but_open_on_exchange', positions['CRCLUSDT']['classification'])
        self.assertEqual(positions['CRCLUSDT']['severity'], 'ERROR')

    def test_managed_open_short_with_matching_open_trade_is_not_desynced(self):
        with tempfile.TemporaryDirectory() as tmp:
            trades = os.path.join(tmp, 'trades.jsonl')
            trade_id = 'short_CRCLUSDT_1783540416'
            self._write_trades(trades, [
                {'event_type': 'TRADE_OPEN', 'trade_id': trade_id, 'symbol': 'CRCLUSDT', 'side': 'SHORT', 'status': 'OPEN', 'opened_at': '2026-07-08T12:00:00Z'},
            ])

            positions = futures_reconciliation.classify_positions(
                [self._raw_binance_short('CRCLUSDT')],
                state={'positions': [self._managed_state_short('CRCLUSDT', trade_id=trade_id)]},
                open_orders_by_symbol={'CRCLUSDT': [self._reduce_only_tp_order('CRCLUSDT')]},
                trades_file=trades,
            )

        entry = positions['CRCLUSDT']
        self.assertTrue(entry['managed_in_state'])
        self.assertTrue(entry['has_open_orders'])
        self.assertEqual(entry['open_orders_count'], 1)
        self.assertEqual(entry['history_trade_status'], 'OPEN')
        self.assertNotIn('desynced_closed_but_open_on_exchange', entry['classification'])
        self.assertNotIn('unprotected_futures_position', entry['classification'])
        self.assertEqual(entry['severity'], 'INFO')
        self.assertEqual(entry['suggested_action'], 'none')

    def test_two_managed_open_shorts_are_aligned(self):
        with tempfile.TemporaryDirectory() as tmp:
            trades = os.path.join(tmp, 'trades.jsonl')
            zec_id = 'short_ZECUSDT_1783533464'
            crcl_id = 'short_CRCLUSDT_1783540416'
            self._write_trades(trades, [
                {'event_type': 'TRADE_OPEN', 'trade_id': zec_id, 'symbol': 'ZECUSDT', 'side': 'SHORT', 'status': 'OPEN', 'opened_at': '2026-07-08T12:00:00Z'},
                {'event_type': 'TRADE_OPEN', 'trade_id': crcl_id, 'symbol': 'CRCLUSDT', 'side': 'SHORT', 'status': 'OPEN', 'opened_at': '2026-07-08T12:05:00Z'},
            ])
            status = os.path.join(tmp, 'futures_reconciliation_status.json')

            payload = futures_reconciliation.reconcile_observed_positions(
                [self._raw_binance_short('ZECUSDT'), self._raw_binance_short('CRCLUSDT')],
                state={'positions': [
                    self._managed_state_short('ZECUSDT', trade_id=zec_id, quantity=0.067, tp_order_id='803134129964'),
                    self._managed_state_short('CRCLUSDT', trade_id=crcl_id),
                ]},
                open_orders_by_symbol={
                    'ZECUSDT': [self._reduce_only_tp_order('ZECUSDT', '803134129964', quantity='0.067', price='448.14')],
                    'CRCLUSDT': [self._reduce_only_tp_order('CRCLUSDT')],
                },
                trades_file=trades,
                status_file=status,
                allowed_count=2,
            )

        summary = payload['summary']
        self.assertEqual(summary['observed_count'], 2)
        self.assertEqual(summary['managed_count'], 2)
        self.assertEqual(summary['unmanaged_count'], 0)
        self.assertEqual(summary['orphan_count'], 0)
        self.assertEqual(summary['unprotected_count'], 0)
        self.assertEqual(summary['desynced_count'], 0)
        self.assertTrue(summary['aligned'])
        self.assertEqual(summary['status'], 'ALINEADO')

    def test_matching_trade_closed_history_still_marks_desynced(self):
        with tempfile.TemporaryDirectory() as tmp:
            trades = os.path.join(tmp, 'trades.jsonl')
            trade_id = 'short_CRCLUSDT_1783540416'
            self._write_trades(trades, [
                {'event_type': 'TRADE_OPEN', 'trade_id': trade_id, 'symbol': 'CRCLUSDT', 'side': 'SHORT', 'status': 'OPEN', 'opened_at': '2026-07-08T12:00:00Z'},
                {'event_type': 'TRADE_CLOSE', 'trade_id': trade_id, 'symbol': 'CRCLUSDT', 'side': 'SHORT', 'status': 'CLOSED', 'closed_at': '2026-07-08T13:00:00Z'},
            ])

            positions = futures_reconciliation.classify_positions(
                [self._raw_binance_short('CRCLUSDT')],
                state={'positions': [self._managed_state_short('CRCLUSDT', trade_id=trade_id)]},
                open_orders_by_symbol={'CRCLUSDT': [self._reduce_only_tp_order('CRCLUSDT')]},
                trades_file=trades,
            )

        self.assertIn('desynced_closed_but_open_on_exchange', positions['CRCLUSDT']['classification'])
        self.assertEqual(positions['CRCLUSDT']['history_trade_status'], 'CLOSED')

    def test_managed_open_short_without_orders_remains_unprotected(self):
        with tempfile.TemporaryDirectory() as tmp:
            trades = os.path.join(tmp, 'trades.jsonl')
            trade_id = 'short_CRCLUSDT_1783540416'
            self._write_trades(trades, [
                {'event_type': 'TRADE_OPEN', 'trade_id': trade_id, 'symbol': 'CRCLUSDT', 'side': 'SHORT', 'status': 'OPEN'},
            ])

            positions = futures_reconciliation.classify_positions(
                [self._raw_binance_short('CRCLUSDT')],
                state={'positions': [self._managed_state_short('CRCLUSDT', trade_id=trade_id)]},
                open_orders_by_symbol={'CRCLUSDT': []},
                trades_file=trades,
            )

        classes = positions['CRCLUSDT']['classification']
        self.assertIn('managed_futures_position', classes)
        self.assertIn('unprotected_futures_position', classes)
        self.assertNotIn('desynced_closed_but_open_on_exchange', classes)

    def test_old_observed_position_is_stale_observed(self):
        with tempfile.TemporaryDirectory() as tmp:
            trades = os.path.join(tmp, 'trades.jsonl')
            self._write_trades(trades, [
                {'event_type': 'TRADE_OPEN', 'trade_id': 'short_HYPEUSDT_1', 'symbol': 'HYPEUSDT', 'side': 'SHORT', 'opened_at': '2026-01-01T00:00:00Z'},
            ])

            positions = futures_reconciliation.classify_positions(
                [self._observed_short('HYPEUSDT')],
                state={'positions': []},
                open_orders_by_symbol={'HYPEUSDT': []},
                trades_file=trades,
            )

        self.assertIn('stale_observed_futures_position', positions['HYPEUSDT']['classification'])

    def test_persist_status_and_alert_throttle(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = os.path.join(tmp, 'futures_reconciliation_status.json')
            alert = Mock()
            positions = {
                'CRCLUSDT': {
                    'symbol': 'CRCLUSDT',
                    'side': 'SHORT',
                    'position_amt': -0.17,
                    'notional': 11.67,
                    'unrealized_pnl': -0.87,
                    'has_open_orders': False,
                    'managed_in_state': False,
                    'classification': ['observed_futures_position', 'unprotected_futures_position'],
                    'severity': 'ERROR',
                }
            }

            first = futures_reconciliation.persist_reconciliation(positions, status_file=status, alert_fn=alert)
            second = futures_reconciliation.persist_reconciliation(positions, status_file=status, alert_fn=alert)

        self.assertEqual(alert.call_count, 1)
        self.assertIn('CRCLUSDT', first['positions'])
        self.assertEqual(second['summary']['unprotected_count'], 1)

    def test_aligned_reconciliation_does_not_log_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = os.path.join(tmp, 'futures_reconciliation_status.json')
            with patch.object(futures_reconciliation.logging, 'warning') as warning_log, \
                 patch.object(futures_reconciliation.logging, 'debug') as debug_log:
                payload = futures_reconciliation.persist_reconciliation(
                    {},
                    status_file=status,
                    allowed_count=2,
                )

        self.assertEqual(payload['summary']['status'], 'ALINEADO')
        self.assertTrue(payload['summary']['aligned'])
        self.assertEqual(payload['summary']['observed_count'], 0)
        self.assertEqual(payload['summary']['managed_count'], 0)
        self.assertEqual(payload['summary']['unmanaged_count'], 0)
        self.assertEqual(payload['summary']['orphan_count'], 0)
        self.assertEqual(payload['summary']['unprotected_count'], 0)
        self.assertEqual(payload['summary']['desynced_count'], 0)
        self.assertEqual(payload['summary']['allowed_count'], 2)
        warning_log.assert_not_called()
        debug_log.assert_called_once()

    def test_risky_reconciliation_keeps_warning_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = os.path.join(tmp, 'futures_reconciliation_status.json')
            positions = {
                'CRCLUSDT': {
                    'symbol': 'CRCLUSDT',
                    'side': 'SHORT',
                    'position_amt': -0.17,
                    'notional': 11.67,
                    'has_open_orders': False,
                    'managed_in_state': False,
                    'classification': ['observed_futures_position', 'unmanaged_futures_position', 'unprotected_futures_position'],
                    'severity': 'WARNING',
                }
            }
            with patch.object(futures_reconciliation.logging, 'warning') as warning_log:
                payload = futures_reconciliation.persist_reconciliation(
                    positions,
                    status_file=status,
                    allowed_count=0,
                )

        self.assertFalse(payload['summary']['aligned'])
        self.assertEqual(payload['summary']['observed_count'], 1)
        self.assertEqual(payload['summary']['unmanaged_count'], 1)
        warning_log.assert_called_once()
        self.assertIn('FUTURES RECONCILIATION summary', warning_log.call_args.args[0])

    def test_collect_open_orders_does_not_close_positions(self):
        binance = Mock()
        binance.futures_open_orders.return_value = []

        result = futures_reconciliation.collect_open_orders(binance, [self._observed_short('BNBUSDT')])

        self.assertEqual(result, {'BNBUSDT': []})
        binance.futures_open_orders.assert_called_once_with({'symbol': 'BNBUSDT'})
        self.assertFalse(binance.fut_signed.called)
        self.assertFalse(binance.create_futures_order.called)


if __name__ == '__main__':
    unittest.main()
