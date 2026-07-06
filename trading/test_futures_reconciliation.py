#!/usr/bin/env python3
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock

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

    def test_aligned_reconciliation_logs_info_not_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = os.path.join(tmp, 'futures_reconciliation_status.json')
            with self.assertLogs(level='INFO') as logs:
                payload = futures_reconciliation.persist_reconciliation(
                    {},
                    status_file=status,
                    allowed_count=2,
                )

        self.assertEqual(payload['summary']['status'], 'ALINEADO')
        self.assertTrue(payload['summary']['aligned'])
        warning_lines = [line for line in logs.output if line.startswith('WARNING')]
        self.assertEqual(warning_lines, [])
        self.assertTrue(any('FUTURES RECONCILIATION summary' in line for line in logs.output))

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
            with self.assertLogs(level='WARNING') as logs:
                payload = futures_reconciliation.persist_reconciliation(
                    positions,
                    status_file=status,
                    allowed_count=0,
                )

        self.assertFalse(payload['summary']['aligned'])
        self.assertEqual(payload['summary']['observed_count'], 1)
        self.assertEqual(payload['summary']['unmanaged_count'], 1)
        self.assertTrue(any('FUTURES RECONCILIATION summary' in line for line in logs.output))

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
