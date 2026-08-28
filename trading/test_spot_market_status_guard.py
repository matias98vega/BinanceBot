#!/usr/bin/env python3
import copy
import io
import os
import socket
import sys
import unittest
from contextlib import ExitStack
from unittest.mock import patch
from urllib.error import HTTPError

os.environ.setdefault('BINANCE_API_KEY', 'test')
os.environ.setdefault('BINANCE_API_SECRET', 'test')
sys.path.insert(0, os.path.dirname(__file__))

import longs
import utils
import version_history
from testing import FakeBinanceClient, FakeExchangeState


SYMBOL = 'XMRUSDT'
CANDIDATE = {'symbol': SYMBOL, 'sl': 90, 'tp': 110, 'atr': 2, 'score': 8, 'reasons': []}


def http_error(status, code, msg):
    body = ('{"code":%s,"msg":"%s"}' % (code, msg)).encode()
    error = HTTPError('https://api.binance.test/api/v3/order', status, msg, {}, io.BytesIO(body))
    error.binance_body = body.decode()
    error.binance_endpoint = '/api/v3/order'
    error.binance_method = 'POST'
    error.binance_payload = {
        'symbol': SYMBOL, 'side': 'BUY', 'type': 'MARKET', 'quantity': '1',
        'timestamp': 1700000000000, 'signature': 'secret-signature', 'apiKey': 'secret-key',
    }
    return error


class SpotMarketStatusGuardTests(unittest.TestCase):
    def make_client(self, status='TRADING'):
        state = FakeExchangeState()
        state.set_balance('USDT', 100)
        state.set_price(SYMBOL, 10)
        state.set_filters(SYMBOL, tick_size='.01', step_size='.001', min_qty='.001', min_notional='5')
        state.set_filters(SYMBOL, futures=True, tick_size='.01', step_size='.001', min_qty='.001', min_notional='5')
        state.set_spot_status(SYMBOL, status)
        return FakeBinanceClient(state)

    def run_long(self, client, state=None):
        local_state = state if state is not None else {'positions': []}
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(longs, 'BINANCE', client))
        stack.enter_context(patch.object(longs.config, 'DRY_RUN', False))
        stack.enter_context(patch.object(longs.utils, 'get_spot_risk_pct', return_value=.1))
        stack.enter_context(patch.object(longs.utils, 'get_spot_capital_per_position', return_value=10))
        stack.enter_context(patch.object(longs.utils, 'validate_position_capacity', return_value=(True, '', 0, 2)))
        stack.enter_context(patch.object(longs.capital_manager, 'validate_spot_order', return_value=(True, '', {})))
        stack.enter_context(patch.object(longs.decision_timeline, 'record_signal_evaluated'))
        stack.enter_context(patch.object(longs.decision_timeline, 'record_order_event'))
        stack.enter_context(patch.object(longs.decision_timeline, 'record_protection_event'))
        sleep = stack.enter_context(patch.object(longs.time, 'sleep'))
        result = longs.open_long(
            CANDIDATE, local_state, max_longs=2,
            pre_entry_gate_result={'entry_allowed': True},
        )
        return result, sleep

    @staticmethod
    def operations(client, prefix):
        return [call for call in client.calls if call['operation'].startswith(prefix)]

    def test_trading_symbol_preserves_buy_and_oco_flow(self):
        client = self.make_client('TRADING')
        (position, message), sleep = self.run_long(client)
        self.assertIsNotNone(position, message)
        writes = self.operations(client, 'spot_signed:POST')
        self.assertEqual(
            [item['operation'] for item in writes],
            ['spot_signed:POST:/api/v3/order', 'spot_signed:POST:/api/v3/order/oco'],
        )
        sleep.assert_not_called()

    def test_non_trading_statuses_reject_before_price_or_post(self):
        for status in ('BREAK', 'HALT', ''):
            with self.subTest(status=status or 'missing'):
                client = self.make_client(status)
                state = {'positions': [], 'trade_count': 7, 'sentinel': 'unchanged'}
                before = copy.deepcopy(state)
                (position, message), sleep = self.run_long(client, state)
                self.assertIsNone(position)
                self.assertIn('SPOT_SYMBOL_NOT_TRADING', message)
                self.assertIn(status or 'UNKNOWN', message)
                self.assertEqual(state, before)
                self.assertEqual([], self.operations(client, 'get_spot_price'))
                self.assertEqual([], self.operations(client, 'spot_signed:POST'))
                sleep.assert_not_called()

    def test_xmr_spot_closed_even_if_futures_contract_is_available(self):
        client = self.make_client('BREAK')
        futures_info = client.exchange_info('futures', {'symbol': SYMBOL})['symbols'][0]
        self.assertEqual('TRADING', futures_info['status'])
        (position, message), _ = self.run_long(client)
        self.assertIsNone(position)
        self.assertEqual('SPOT_SYMBOL_NOT_TRADING: XMRUSDT status=BREAK', message)
        self.assertEqual([], self.operations(client, 'spot_signed:POST'))

    def test_market_closed_race_is_one_post_no_retry_and_no_protection(self):
        client = self.make_client('TRADING')
        client.state.queue_error(
            'spot_signed:POST:/api/v3/order',
            http_error(400, -1013, 'Market is closed.'),
        )
        state = {'positions': [], 'trade_count': 7, 'sentinel': 'unchanged'}
        before = copy.deepcopy(state)
        (position, message), sleep = self.run_long(client, state)
        self.assertIsNone(position)
        self.assertIn('Binance -1013: Market is closed.', message)
        self.assertEqual(1, len(self.operations(client, 'spot_signed:POST:/api/v3/order')))
        self.assertEqual([], self.operations(client, 'spot_signed:POST:/api/v3/order/oco'))
        self.assertEqual(state, before)
        sleep.assert_not_called()

    def test_deterministic_400_is_not_retried(self):
        client = self.make_client()
        client.state.queue_error(
            'spot_signed:POST:/api/v3/order',
            http_error(400, -1111, 'Precision is over the maximum defined for this asset.'),
        )
        (position, message), sleep = self.run_long(client)
        self.assertIsNone(position)
        self.assertIn('Binance -1111:', message)
        self.assertEqual(1, len(self.operations(client, 'spot_signed:POST:/api/v3/order')))
        sleep.assert_not_called()

    def test_rate_limit_server_and_timeout_are_retried_once(self):
        cases = (
            ('rate-limit', http_error(429, -1003, 'Too many requests.'), 'RATE_LIMIT'),
            ('server', http_error(500, -1000, 'Internal error.'), 'SERVER_ERROR'),
            ('timeout', socket.timeout('timed out'), None),
        )
        for label, error, classification in cases:
            with self.subTest(case=label):
                client = self.make_client()
                client.state.queue_error('spot_signed:POST:/api/v3/order', error)
                (position, message), sleep = self.run_long(client)
                self.assertIsNotNone(position, message)
                self.assertEqual(2, sum(call['operation'] == 'spot_signed:POST:/api/v3/order' for call in client.calls))
                sleep.assert_any_call(10)
                if classification:
                    self.assertEqual(classification, utils.extract_http_error_details(error)['classification'])

    def test_structured_error_classification_and_context_are_sanitized(self):
        cases = (
            (-1013, 'Market is closed.', 'MARKET_CLOSED'),
            (-1013, 'Filter failure: LOT_SIZE', 'LOT_SIZE/FILTER'),
            (-1013, 'Filter failure: MIN_NOTIONAL', 'MIN_NOTIONAL/FILTER'),
            (-1111, 'Precision is over the maximum defined for this asset.', 'PRECISION/QUANTITY'),
        )
        for code, message, expected in cases:
            with self.subTest(expected=expected):
                details = utils.extract_http_error_details(http_error(400, code, message))
                self.assertEqual(expected, details['classification'])
                self.assertFalse(details['retryable'])
                self.assertEqual(details['context'], details['payload'])
                self.assertEqual(
                    {'symbol': SYMBOL, 'side': 'BUY', 'type': 'MARKET', 'quantity': '1'},
                    details['context'],
                )
                rendered = utils.format_binance_error_for_user(http_error(400, code, message))
                self.assertNotIn('secret', rendered)
                self.assertNotIn('signature', rendered)

    def test_production_filter_parser_preserves_status_and_quantity_metadata(self):
        info = {
            'symbols': [{
                'symbol': SYMBOL, 'status': 'BREAK', 'baseAssetPrecision': 8, 'quotePrecision': 8,
                'filters': [
                    {'filterType': 'PRICE_FILTER', 'tickSize': '0.01'},
                    {'filterType': 'LOT_SIZE', 'stepSize': '0.001', 'minQty': '0.001', 'maxQty': '1000'},
                    {'filterType': 'MARKET_LOT_SIZE', 'stepSize': '0.01', 'minQty': '0.01', 'maxQty': '500'},
                    {'filterType': 'NOTIONAL', 'minNotional': '5', 'maxNotional': '25000'},
                ],
            }],
        }
        utils._spot_info_cache.pop(SYMBOL, None)
        self.addCleanup(utils._spot_info_cache.pop, SYMBOL, None)
        with patch.object(utils, 'spot_public', return_value=info) as spot_public:
            parsed = utils.get_spot_filters(SYMBOL)
        spot_public.assert_called_once_with('/api/v3/exchangeInfo', {'symbol': SYMBOL})
        self.assertEqual('BREAK', parsed['status'])
        self.assertEqual(1000.0, parsed['max_qty'])
        self.assertEqual(0.01, parsed['market_step_size'])
        self.assertEqual(500.0, parsed['market_max_qty'])
        self.assertEqual(25000.0, parsed['max_notional'])

    def test_operator_alert_preserves_binance_details_without_secrets(self):
        from orchestration import cycle_runner
        error = http_error(400, -1013, 'Market is closed.')
        message = 'Error al comprar XMRUSDT tras 1 intento: ' + utils.format_binance_error_for_user(error)
        alert = cycle_runner.format_open_failure_alert('LONG', SYMBOL, message)
        self.assertIn('FALLÓ apertura LONG XMRUSDT', alert)
        self.assertIn('-1013', alert)
        self.assertIn('Market is closed.', alert)
        self.assertNotIn('secret', alert)
        self.assertNotIn('signature', alert)

    def test_fake_exchange_info_exposes_status_and_complete_quantity_metadata(self):
        client = self.make_client('BREAK')
        info = client.exchange_info('spot', {'symbol': SYMBOL})['symbols'][0]
        filter_types = {item['filterType'] for item in info['filters']}
        parsed = client.get_spot_filters(SYMBOL)
        self.assertEqual('BREAK', info['status'])
        self.assertEqual('BREAK', parsed['status'])
        self.assertEqual({'PRICE_FILTER', 'LOT_SIZE', 'MARKET_LOT_SIZE', 'MIN_NOTIONAL'}, filter_types)
        for key in ('step_size', 'min_qty', 'max_qty', 'market_step_size', 'market_min_qty', 'market_max_qty'):
            self.assertIn(key, parsed)

    def test_short_fake_order_contract_is_unaffected_by_spot_status(self):
        client = self.make_client('BREAK')
        order = client.create_futures_order({
            'symbol': SYMBOL, 'side': 'SELL', 'type': 'MARKET', 'quantity': '1',
        })
        self.assertEqual('FILLED', order['status'])
        self.assertEqual('-1', str(client.state.futures_positions[SYMBOL]['positionAmt']))

    def test_new_records_use_current_version_and_existing_opening_version_is_preserved(self):
        current = version_history.attach_version_metadata({})
        historical = version_history.attach_version_metadata({'bot_version': 'v1.0-alpha'})
        self.assertEqual('v1.6-preventive-spot-close-fix', current['bot_version'])
        self.assertEqual('v1.0-alpha', historical['bot_version'])


if __name__ == '__main__':
    unittest.main()
