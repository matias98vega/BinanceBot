#!/usr/bin/env python3
import os
import sys
import io
import importlib.util
import json
import tempfile
import urllib.error
import unittest
from unittest.mock import patch

os.environ.setdefault('BINANCE_API_KEY', 'test')
os.environ.setdefault('BINANCE_API_SECRET', 'test')

sys.path.insert(0, os.path.dirname(__file__))

import rebalance
import telegram_commands


PROJECT_DIR = os.path.dirname(os.path.dirname(__file__))


def http_error(status=400, body='{"code":-2010,"msg":"Insufficient balance"}'):
    err = urllib.error.HTTPError(
        url='https://api.binance.com/sapi/v1/asset/transfer',
        code=status,
        msg='Bad Request',
        hdrs={},
        fp=io.BytesIO(body.encode('utf-8')),
    )
    err.binance_endpoint = '/sapi/v1/asset/transfer'
    err.binance_method = 'POST'
    err.binance_payload = {'type': 'MAIN_UMFUTURE', 'asset': 'USDT', 'amount': '26.94'}
    return err


class FakeBinance:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def spot_signed(self, method, path, params):
        self.calls.append((method, path, dict(params)))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeRebalanceAccount:
    def get_usdt_spot(self):
        return 31.85

    def fut_signed(self, method, path, params):
        return {
            'totalWalletBalance': '22.16',
            'totalUnrealizedProfit': '0',
            'availableBalance': '0.00',
            'totalPositionInitialMargin': '20.42',
            'positions': [
                {'symbol': 'CRCLUSDT', 'positionAmt': '-1'},
                {'symbol': 'SUIUSDT', 'positionAmt': '-1'},
                {'symbol': 'NEARUSDT', 'positionAmt': '-1'},
                {'symbol': 'HYPEUSDT', 'positionAmt': '-1'},
                {'symbol': 'BNBUSDT', 'positionAmt': '-1'},
            ],
        }


class RebalanceReserveTests(unittest.TestCase):
    def test_transfer_amount_with_zero_wallet_reserve(self):
        amount = rebalance._transferable_amount(
            required_amount=51.41,
            source_free=51.41,
            wallet_min=0,
        )
        self.assertEqual(amount, 51.41)

    def test_transfer_amount_with_configured_wallet_reserve(self):
        amount = rebalance._transferable_amount(
            required_amount=51.41,
            source_free=51.41,
            wallet_min=3,
        )
        self.assertEqual(amount, 48.41)

    def test_transfer_buffer_applied_and_never_negative(self):
        self.assertEqual(rebalance._apply_transfer_buffer(51.41, buffer=0.10), 51.31)
        self.assertEqual(rebalance._apply_transfer_buffer(0.05, buffer=0.10), 0.0)


class RebalanceDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.status_file = os.path.join(self.tmpdir.name, 'rebalance_status.json')
        self.status_patch = patch.object(rebalance, 'REBALANCE_STATUS_FILE', self.status_file)
        self.status_patch.start()

    def tearDown(self):
        self.status_patch.stop()
        self.tmpdir.cleanup()

    def read_status(self):
        with open(self.status_file, encoding='utf-8') as f:
            return json.load(f)

    def test_rebalance_log_can_emit_info_for_benign_messages(self):
        with patch.object(rebalance.logging, 'info') as info_log, \
             patch.object(rebalance.logging, 'warning') as warning_log, \
             patch.object(rebalance.decision_timeline, 'record_rebalance_event'):
            rebalance._rebalance_log('SKIP: reason=balances aligned', level='INFO')

        info_log.assert_called_once()
        warning_log.assert_not_called()
        self.assertIn('REBALANCE SKIP: reason=balances aligned', info_log.call_args.args[0])

    def test_rebalance_log_defaults_to_warning_for_unclassified_messages(self):
        with patch.object(rebalance.logging, 'warning') as warning_log, \
             patch.object(rebalance.logging, 'info') as info_log, \
             patch.object(rebalance.decision_timeline, 'record_rebalance_event'):
            rebalance._rebalance_log('SKIP: reason=active shorts')

        warning_log.assert_called_once()
        info_log.assert_not_called()

    def test_persists_rebalance_failure_details(self):
        with patch.object(rebalance.decision_timeline, 'record_rebalance_event') as timeline:
            status, details = rebalance._record_rebalance_failure(
                'SPOT_TO_FUTURES',
                26.94,
                http_error(),
                {'type': 'MAIN_UMFUTURE', 'asset': 'USDT', 'amount': '26.94'},
            )

        saved = self.read_status()
        self.assertTrue(saved['pending'])
        self.assertEqual(saved['direction'], 'SPOT_TO_FUTURES')
        self.assertEqual(saved['amount'], 26.94)
        self.assertEqual(saved['attempts'], 1)
        self.assertEqual(saved['last_http_status'], 400)
        self.assertEqual(saved['last_binance_code'], -2010)
        self.assertEqual(saved['last_message'], 'Insufficient balance')
        self.assertEqual(saved['last_error'], 'Insufficient balance')
        self.assertIn('Insufficient balance', saved['last_raw_body'])
        self.assertEqual(details['payload']['amount'], '26.94')
        timeline.assert_called()
        args, kwargs = timeline.call_args
        self.assertEqual(args[0], 'rebalance_error')
        self.assertIn('intento #1', args[1])
        self.assertIn('Insufficient balance', args[1])
        self.assertEqual(kwargs['details']['attempts'], 1)
        self.assertEqual(kwargs['details']['reason'], 'Insufficient balance')
        self.assertEqual(kwargs['details']['binance_code'], -2010)

    def test_failure_attempts_increment(self):
        with patch.object(rebalance.decision_timeline, 'record_rebalance_event'):
            rebalance._record_rebalance_failure('SPOT_TO_FUTURES', 26.94, http_error(), {})
            rebalance._record_rebalance_failure('SPOT_TO_FUTURES', 26.94, http_error(), {})

        self.assertEqual(self.read_status()['attempts'], 2)

    def test_pending_check_without_transfer_records_reason(self):
        with patch.object(rebalance.decision_timeline, 'record_rebalance_event') as timeline:
            status = rebalance._record_rebalance_pending_check(
                'FUTURES_TO_SPOT',
                22.16,
                'Pendiente, pero no transferido porque Futures esta ocupado por shorts activos',
                blocked_reason='active_shorts',
                context={'active_shorts': 2, 'fut_free': 1.5},
                status='BLOCKED',
            )

        saved = self.read_status()
        self.assertTrue(status['pending'])
        self.assertTrue(saved['pending'])
        self.assertEqual(saved['direction'], 'FUTURES_TO_SPOT')
        self.assertEqual(saved['amount'], 22.16)
        self.assertEqual(saved['attempts'], 0)
        self.assertIn('last_check', saved)
        self.assertIsNone(saved['last_attempt'])
        self.assertEqual(saved['blocked_reason'], 'active_shorts')
        self.assertIn('Futures esta ocupado', saved['pending_reason'])
        timeline.assert_called_once()
        args, kwargs = timeline.call_args
        self.assertEqual(args[0], 'rebalance_blocked')
        self.assertEqual(kwargs['details']['attempts'], 0)

    def test_pending_created_event_without_block(self):
        with patch.object(rebalance.decision_timeline, 'record_rebalance_event') as timeline:
            rebalance._record_rebalance_pending_check(
                'FUTURES_TO_SPOT',
                22.16,
                'Capital aun fuera de tolerancia de alineacion',
                context={'spot_real': 27.1, 'futures_real': 26.9, 'target_spot': 49.26, 'target_futures': 4.74},
            )

        saved = self.read_status()
        self.assertTrue(saved['pending'])
        self.assertEqual(saved['pending_reason'], 'Capital aun fuera de tolerancia de alineacion')
        args, kwargs = timeline.call_args
        self.assertEqual(args[0], 'rebalance_pending_created')
        self.assertEqual(kwargs['details']['reason'], 'Capital aun fuera de tolerancia de alineacion')
        self.assertEqual(kwargs['details']['spot_real'], 27.1)

    def test_clear_rebalance_status_after_success(self):
        with patch.object(rebalance.decision_timeline, 'record_rebalance_event'):
            rebalance._record_rebalance_failure('SPOT_TO_FUTURES', 26.94, http_error(), {})

        cleared = rebalance.clear_rebalance_status()

        self.assertFalse(cleared['pending'])
        self.assertFalse(self.read_status()['pending'])
        self.assertEqual(self.read_status()['attempts'], 0)

    def test_error_message_preserves_binance_code_and_msg(self):
        with patch.object(rebalance.decision_timeline, 'record_rebalance_event'):
            _, details = rebalance._record_rebalance_failure('SPOT_TO_FUTURES', 26.94, http_error(), {})

        message = rebalance._format_transfer_error('SPOT_TO_FUTURES', details, RuntimeError('HTTP Error 400'))

        self.assertIn('HTTP 400', message)
        self.assertIn('code=-2010', message)
        self.assertIn('Insufficient balance', message)

    def test_telegram_capital_shows_rebalance_reason(self):
        metrics = {
            'total_real': 54.0,
            'total_limit': 54.0,
            'total_authorized': 54.0,
            'spot_real': 26.9,
            'spot_target': 0.0,
            'spot_used': 8.4,
            'spot_reserved': 0,
            'futures_real': 27.1,
            'futures_target': 54.0,
            'futures_used': 18.2,
            'futures_reserved': 0,
            'rebalance': {
                'status': 'PENDING',
                'direction': 'SPOT_TO_FUTURES',
                'amount_pending': 26.94,
                'attempts': 17,
                'last_check': '2026-06-30T18:31:00Z',
                'last_attempt': '2026-06-30T18:32:00Z',
                'last_http_status': 400,
                'last_binance_code': -2010,
                'last_message': 'Insufficient balance',
                'buffer_applied': 0.10,
            },
            'max_exposure_percent': 80.0,
            'max_position_percent': None,
            'warning': None,
        }

        with patch.object(telegram_commands, '_exposure_metrics', return_value=metrics):
            text = telegram_commands._render_page('capital')['text']

        self.assertIn('Estado:', text)
        self.assertIn('Pendiente', text)
        self.assertIn('Direccion: Spot', text)
        self.assertIn('Desbalance: 26.94 USDT', text)
        self.assertIn('Buffer aplicado: 0.10 USDT', text)
        self.assertIn('Intentos: 17', text)
        self.assertIn('Ultimo check:', text)
        self.assertIn('HTTP 400', text)
        self.assertIn('code=-2010', text)
        self.assertIn('Insufficient balance', text)

    def test_rebalance_attempt_event_is_recorded_before_transfer(self):
        fake = FakeBinance([{'tranId': 1}])
        with patch.object(rebalance, 'BINANCE', fake), \
             patch.object(rebalance.decision_timeline, 'record_rebalance_event') as timeline:
            ok, amount, meta = rebalance._transfer_with_recovery(
                'FUTURES_TO_SPOT',
                22.16,
                context={'spot_real': 27.1, 'futures_real': 26.9, 'target_spot': 49.26, 'target_futures': 4.74},
            )

        self.assertTrue(ok)
        self.assertEqual(amount, 22.16)
        self.assertEqual(meta['attempts'], 1)
        self.assertEqual(fake.calls[0][2]['amount'], '22.16')
        self.assertEqual(timeline.call_args_list[0].args[0], 'rebalance_attempt')
        self.assertEqual(timeline.call_args_list[0].kwargs['details']['amount'], 22.16)
        self.assertEqual(timeline.call_args_list[0].kwargs['details']['target_spot'], 49.26)

    def test_rebalance_attempt_logs_info_not_warning(self):
        with patch.object(rebalance.logging, 'info') as info_log, \
             patch.object(rebalance.logging, 'warning') as warning_log, \
             patch.object(rebalance.decision_timeline, 'record_rebalance_event'):
            rebalance._record_rebalance_attempt('FUTURES_TO_SPOT', 22.16, 1, context={})

        self.assertTrue(any('REBALANCE ATTEMPT' in call.args[0] for call in info_log.call_args_list))
        self.assertFalse(any('REBALANCE ATTEMPT' in call.args[0] for call in warning_log.call_args_list))

    def test_telegram_capital_shows_pending_without_attempt_reason(self):
        metrics = {
            'total_real': 54.0,
            'total_limit': 54.0,
            'total_authorized': 54.0,
            'spot_real': 27.1,
            'spot_target': 49.26,
            'spot_used': 0.0,
            'spot_reserved': 0,
            'futures_real': 26.9,
            'futures_target': 4.74,
            'futures_used': 18.2,
            'futures_reserved': 0,
            'rebalance': {
                'status': 'PENDING',
                'direction': 'FUTURES_TO_SPOT',
                'amount_pending': 22.16,
                'attempts': 0,
                'last_check': '2026-07-02T15:32:00Z',
                'last_attempt': None,
                'pending_reason': 'Pendiente, pero no transferido porque Futures esta ocupado por shorts activos',
                'blocked_reason': 'active_shorts',
            },
            'max_exposure_percent': 80.0,
            'max_position_percent': None,
            'warning': None,
        }

        with patch.object(telegram_commands, '_exposure_metrics', return_value=metrics):
            text = telegram_commands._render_page('capital')['text']

        self.assertIn('Estado:', text)
        self.assertIn('Pendiente', text)
        self.assertIn('Direccion: Futures', text)
        self.assertIn('Desbalance: 22.16 USDT', text)
        self.assertIn('Intentos: 0', text)
        self.assertIn('Ultimo check:', text)
        self.assertIn('Ultimo intento: No disponible', text)
        self.assertIn('Motivo:', text)
        self.assertIn('Futures esta ocupado', text)
        self.assertIn('Bloqueo: active_shorts', text)

    def test_pending_amount_is_distinct_from_transferable_amount_when_futures_margin_blocks(self):
        with patch.object(rebalance.decision_timeline, 'record_rebalance_event') as timeline:
            status = rebalance._record_rebalance_pending_check(
                'FUTURES_TO_SPOT',
                20.21,
                'Pendiente, pero no transferido porque Futures esta ocupado por shorts activos',
                blocked_reason='active_shorts',
                context={
                    'pending_amount': 20.21,
                    'transferable_amount': 0.0,
                    'available_balance': 0.0,
                    'position_margin': 20.42,
                    'wallet_balance': 22.16,
                },
                status='BLOCKED',
            )

        saved = self.read_status()
        self.assertEqual(status['amount'], 20.21)
        self.assertEqual(saved['amount'], 20.21)
        self.assertEqual(saved['transferable_amount'], 0.0)
        self.assertEqual(saved['available_balance'], 0.0)
        self.assertEqual(saved['position_margin'], 20.42)
        self.assertEqual(timeline.call_args.args[0], 'rebalance_blocked_margin')

    def test_telegram_pending_margin_block_does_not_show_zero_as_pending_amount(self):
        metrics = {
            'total_real': 54.01,
            'total_limit': 54.01,
            'total_authorized': 54.01,
            'spot_real': 31.85,
            'spot_target': 52.06,
            'spot_used': 0.0,
            'spot_reserved': 0,
            'futures_real': 22.16,
            'futures_target': 1.95,
            'futures_used': 20.42,
            'futures_position_margin': 20.42,
            'futures_available_balance': 0.0,
            'futures_reserved': 0,
            'rebalance': {
                'status': 'PENDING',
                'direction': 'FUTURES_TO_SPOT',
                'amount_pending': 20.21,
                'transferable_amount': 0.0,
                'available_balance': 0.0,
                'position_margin': 20.42,
                'wallet_balance': 22.16,
                'attempts': 0,
                'pending_reason': 'Pendiente, pero no transferido porque Futures esta ocupado por shorts activos',
                'blocked_reason': 'active_shorts',
            },
            'max_exposure_percent': 80.0,
            'max_position_percent': None,
            'warning': None,
        }

        with patch.object(telegram_commands, '_exposure_metrics', return_value=metrics):
            text = telegram_commands._render_page('capital')['text']

        self.assertIn('Desbalance: 20.21 USDT', text)
        self.assertIn('Transferible: 0.00 USDT', text)
        self.assertIn('Capital Futures comprometido: 20.42 USDT', text)
        self.assertNotIn('Monto:\n0.00 USDT', text)

    def test_available_balance_zero_blocks_transfer_without_losing_pending_amount(self):
        state = {
            'last_rebalance_trend': 'bearish',
            'positions': [{'direction': 'short', 'symbol': 'CRCLUSDT', 'entry_price': 1, 'quantity': 1}],
        }
        with patch.object(rebalance, 'BINANCE', FakeRebalanceAccount()), \
             patch.object(rebalance.config, 'DIRECTIONAL_MODE', True), \
             patch.object(rebalance.capital_manager, 'cap_transfer_amount', return_value=0.0), \
             patch.object(rebalance.decision_timeline, 'record_rebalance_event') as timeline:
            ok, message = rebalance.rebalance(state, btc_ctx={'trend': 'bullish'})

        saved = self.read_status()
        self.assertFalse(ok)
        self.assertIn('futures', message.lower())
        self.assertTrue(saved['pending'])
        self.assertGreater(saved['amount'], 20.0)
        self.assertEqual(saved['transferable_amount'], 0.0)
        self.assertEqual(saved['available_balance'], 0.0)
        self.assertEqual(saved['position_margin'], 20.42)
        self.assertTrue(any(call.args[0] == 'rebalance_blocked_margin' for call in timeline.call_args_list))

    def test_transfer_success_first_attempt(self):
        fake = FakeBinance([{'tranId': 1}])
        with patch.object(rebalance, 'BINANCE', fake), \
             patch.object(rebalance, 'REBALANCE_TRANSFER_BUFFER_USDT', 0.10), \
             patch.object(rebalance.decision_timeline, 'record_rebalance_event'):
            ok, amount, meta = rebalance._transfer_with_recovery('SPOT_TO_FUTURES', 26.84)

        self.assertTrue(ok)
        self.assertEqual(amount, 26.84)
        self.assertEqual(meta['attempts'], 1)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0][2]['amount'], '26.84')
        self.assertFalse(self.read_status()['pending'])

    def test_transfer_success_logs_info_not_warning(self):
        fake = FakeBinance([{'tranId': 1}])
        with patch.object(rebalance, 'BINANCE', fake), \
             patch.object(rebalance, 'REBALANCE_TRANSFER_BUFFER_USDT', 0.10), \
             patch.object(rebalance.decision_timeline, 'record_rebalance_event'), \
             patch.object(rebalance.logging, 'info') as info_log, \
             patch.object(rebalance.logging, 'warning') as warning_log:
            ok, amount, meta = rebalance._transfer_with_recovery('SPOT_TO_FUTURES', 26.84)

        self.assertTrue(ok)
        info_messages = [call.args[0] for call in info_log.call_args_list]
        warning_messages = [call.args[0] for call in warning_log.call_args_list]
        self.assertTrue(any('REBALANCE TRANSFER attempt=1' in message for message in info_messages))
        self.assertTrue(any('REBALANCE TRANSFER result=success' in message for message in info_messages))
        self.assertFalse(any('REBALANCE TRANSFER attempt=1' in message for message in warning_messages))
        self.assertFalse(any('REBALANCE TRANSFER result=success' in message for message in warning_messages))

    def test_transfer_recovers_on_minus_5013_second_attempt(self):
        fake = FakeBinance([
            http_error(body='{"code":-5013,"msg":"Asset transfer failed: insufficient balance"}'),
            {'tranId': 2},
        ])
        with patch.object(rebalance, 'BINANCE', fake), \
             patch.object(rebalance, 'REBALANCE_TRANSFER_BUFFER_USDT', 0.10), \
             patch.object(rebalance.decision_timeline, 'record_rebalance_event') as timeline:
            ok, amount, meta = rebalance._transfer_with_recovery('SPOT_TO_FUTURES', 26.84)

        self.assertTrue(ok)
        self.assertEqual(amount, 26.74)
        self.assertTrue(meta['recovered'])
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(fake.calls[0][2]['amount'], '26.84')
        self.assertEqual(fake.calls[1][2]['amount'], '26.74')
        saved = self.read_status()
        self.assertFalse(saved['pending'])
        self.assertTrue(saved['recovered'])
        self.assertEqual(saved['final_amount'], 26.74)
        messages = [call.args[1] for call in timeline.call_args_list]
        self.assertTrue(any('Rebalance recuperado automaticamente' in message for message in messages))

    def test_transfer_double_failure_keeps_pending(self):
        fake = FakeBinance([
            http_error(body='{"code":-5013,"msg":"Asset transfer failed: insufficient balance"}'),
            http_error(body='{"code":-5013,"msg":"Asset transfer failed: insufficient balance"}'),
        ])
        with patch.object(rebalance, 'BINANCE', fake), \
             patch.object(rebalance, 'REBALANCE_TRANSFER_BUFFER_USDT', 0.10), \
             patch.object(rebalance.decision_timeline, 'record_rebalance_event') as timeline:
            ok, message, meta = rebalance._transfer_with_recovery('SPOT_TO_FUTURES', 26.84)

        self.assertFalse(ok)
        self.assertIn('code=-5013', message)
        self.assertEqual(meta['attempts'], 2)
        self.assertEqual(len(fake.calls), 2)
        saved = self.read_status()
        self.assertTrue(saved['pending'])
        self.assertEqual(saved['attempts'], 2)
        self.assertEqual(saved['requested_amount'], 26.84)
        self.assertEqual(saved['retried_amount'], 26.74)
        self.assertEqual(saved['buffer_applied'], 0.10)
        messages = [call.args[1] for call in timeline.call_args_list]
        self.assertTrue(any('Rebalance pendiente' in message for message in messages))

    def test_transfer_does_not_retry_other_errors(self):
        fake = FakeBinance([
            http_error(body='{"code":-2010,"msg":"Other failure"}'),
            {'tranId': 3},
        ])
        with patch.object(rebalance, 'BINANCE', fake), \
             patch.object(rebalance, 'REBALANCE_TRANSFER_BUFFER_USDT', 0.10), \
             patch.object(rebalance.decision_timeline, 'record_rebalance_event'):
            ok, message, meta = rebalance._transfer_with_recovery('SPOT_TO_FUTURES', 26.84)

        self.assertFalse(ok)
        self.assertIn('code=-2010', message)
        self.assertEqual(meta['attempts'], 1)
        self.assertEqual(len(fake.calls), 1)

    def test_telegram_capital_shows_auto_recovered_rebalance(self):
        metrics = {
            'total_real': 54.0,
            'total_limit': 54.0,
            'total_authorized': 54.0,
            'spot_real': 26.9,
            'spot_target': 0.0,
            'spot_used': 8.4,
            'spot_reserved': 0,
            'futures_real': 27.1,
            'futures_target': 54.0,
            'futures_used': 18.2,
            'futures_reserved': 0,
            'rebalance': {
                'status': 'NOT_REQUIRED',
                'direction': 'NONE',
                'amount_pending': 0,
                'recovered': True,
                'final_amount': 26.74,
                'buffer_applied': 0.10,
            },
            'max_exposure_percent': 80.0,
            'max_position_percent': None,
            'warning': None,
        }

        with patch.object(telegram_commands, '_exposure_metrics', return_value=metrics):
            text = telegram_commands._render_page('capital')['text']

        self.assertIn('Recuperado autom', text)
        self.assertIn('Monto final:\n26.74 USDT', text)
        self.assertIn('Buffer aplicado:\n0.10 USDT', text)

    def test_reconcile_pending_status_when_capital_aligned(self):
        with patch.object(rebalance.decision_timeline, 'record_rebalance_event'):
            rebalance._record_rebalance_failure(
                'SPOT_TO_FUTURES',
                26.94,
                http_error(body='{"code":-5013,"msg":"Asset transfer failed: insufficient balance"}'),
                {},
            )

        with patch.object(rebalance.decision_timeline, 'record_rebalance_event') as timeline:
            resolved = rebalance.reconcile_rebalance_status_if_aligned(
                spot_actual=0.10,
                fut_actual=53.91,
                target_spot=0.0,
                target_fut=54.01,
                tolerance=0.20,
            )

        saved = self.read_status()
        self.assertIsNotNone(resolved)
        self.assertFalse(saved['pending'])
        self.assertEqual(saved['resolved_reason'], 'capital_already_aligned')
        self.assertIn('last_resolved_at', saved)
        self.assertEqual(saved['last_direction'], 'SPOT_TO_FUTURES')
        self.assertEqual(saved['last_amount'], 26.94)
        self.assertEqual(saved['last_attempts'], 1)
        self.assertIsNone(saved['last_binance_code'])
        timeline.assert_called_once()
        args, kwargs = timeline.call_args
        self.assertEqual(args[0], 'rebalance_reconciled')
        self.assertIn('capital ya alineado', args[1])
        self.assertEqual(kwargs['details']['diff_futures'], 0.1)
        self.assertEqual(kwargs['details']['tolerance'], 0.2)

    def test_reconcile_aligned_logs_info_not_warning(self):
        with patch.object(rebalance.decision_timeline, 'record_rebalance_event'):
            rebalance._record_rebalance_failure(
                'SPOT_TO_FUTURES',
                26.94,
                http_error(body='{"code":-5013,"msg":"Asset transfer failed: insufficient balance"}'),
                {},
            )

        with patch.object(rebalance.decision_timeline, 'record_rebalance_event'), \
             patch.object(rebalance.logging, 'info') as info_log, \
             patch.object(rebalance.logging, 'warning') as warning_log:
            resolved = rebalance.reconcile_rebalance_status_if_aligned(
                spot_actual=0.10,
                fut_actual=53.91,
                target_spot=0.0,
                target_fut=54.01,
                tolerance=0.20,
            )

        self.assertIsNotNone(resolved)
        info_messages = [call.args[0] for call in info_log.call_args_list]
        warning_messages = [call.args[0] for call in warning_log.call_args_list]
        self.assertTrue(any('REBALANCE RECONCILE CHECK' in message for message in info_messages))
        self.assertTrue(any('REBALANCE RECONCILED' in message for message in info_messages))
        self.assertFalse(any('REBALANCE RECONCILE CHECK' in message for message in warning_messages))
        self.assertFalse(any('REBALANCE RECONCILED' in message for message in warning_messages))

    def test_reconcile_keeps_pending_when_outside_tolerance(self):
        with patch.object(rebalance.decision_timeline, 'record_rebalance_event'):
            rebalance._record_rebalance_failure(
                'SPOT_TO_FUTURES',
                26.94,
                http_error(body='{"code":-5013,"msg":"Asset transfer failed: insufficient balance"}'),
                {},
            )

        with patch.object(rebalance.decision_timeline, 'record_rebalance_event') as timeline:
            resolved = rebalance.reconcile_rebalance_status_if_aligned(
                spot_actual=4.0,
                fut_actual=50.0,
                target_spot=0.0,
                target_fut=54.0,
                tolerance=0.20,
            )

        saved = self.read_status()
        self.assertIsNone(resolved)
        self.assertTrue(saved['pending'])
        self.assertEqual(saved['last_binance_code'], -5013)
        timeline.assert_called_once()
        args, kwargs = timeline.call_args
        self.assertEqual(args[0], 'rebalance_pending_check')
        self.assertEqual(kwargs['details']['diff_futures'], 4.0)

    def test_reconciliation_does_not_change_targets(self):
        with patch.object(rebalance.decision_timeline, 'record_rebalance_event'):
            rebalance._record_rebalance_failure('SPOT_TO_FUTURES', 26.94, http_error(), {})
        target_spot = 0.0
        target_fut = 54.01

        resolved = rebalance.reconcile_rebalance_status_if_aligned(
            spot_actual=0.10,
            fut_actual=53.91,
            target_spot=target_spot,
            target_fut=target_fut,
            tolerance=0.20,
        )

        self.assertEqual(target_spot, 0.0)
        self.assertEqual(target_fut, 54.01)
        self.assertEqual(resolved['target_spot'], 0.0)
        self.assertEqual(resolved['target_futures'], 54.01)

    def test_telegram_does_not_show_pending_after_reconciliation(self):
        metrics = {
            'total_real': 54.01,
            'total_limit': 54.01,
            'total_authorized': 54.01,
            'spot_real': 0.10,
            'spot_target': 0.0,
            'spot_used': 0.0,
            'spot_reserved': 0,
            'futures_real': 53.91,
            'futures_target': 54.01,
            'futures_used': 18.2,
            'futures_reserved': 0,
            'rebalance': {
                'status': 'NOT_REQUIRED',
                'direction': 'NONE',
                'amount_pending': 0,
                'reconciled': True,
                'resolved_reason': 'capital_already_aligned',
                'tolerance': 0.20,
            },
            'max_exposure_percent': 80.0,
            'max_position_percent': None,
            'warning': None,
        }

        with patch.object(telegram_commands, '_exposure_metrics', return_value=metrics):
            text = telegram_commands._render_page('capital')['text']

        self.assertIn('Rebalance reconciliado autom', text)
        self.assertIn('Capital alineado dentro de la tolerancia.', text)
        self.assertNotIn('Rebalance pendiente', text)
        self.assertNotIn('Intentos:', text)

    def test_dashboard_rebalance_endpoint_returns_pending_false(self):
        rebalance._write_rebalance_status({
            'pending': False,
            'last_resolved_at': '2026-06-30T18:00:00Z',
            'resolved_reason': 'capital_already_aligned',
        })
        dashboard_path = os.path.join(PROJECT_DIR, 'dashboard', 'app.py')
        spec = importlib.util.spec_from_file_location('dashboard_app_for_rebalance_test', dashboard_path)
        dashboard_app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dashboard_app)

        with patch.object(dashboard_app, 'REBALANCE_STATUS_FILE', self.status_file):
            payload = dashboard_app._api_payload('/api/rebalance')

        self.assertFalse(payload['pending'])
        self.assertEqual(payload['resolved_reason'], 'capital_already_aligned')


if __name__ == '__main__':
    unittest.main()
