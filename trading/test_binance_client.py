#!/usr/bin/env python3
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import binance_client


class BinanceClientTests(unittest.TestCase):
    def setUp(self):
        self.client = binance_client.BinanceClient()

    @patch('utils.spot_signed')
    def test_create_spot_order_reuses_utils_with_identical_payload(self, spot_signed):
        payload = {'symbol': 'ETHUSDT', 'side': 'BUY', 'type': 'MARKET', 'quantity': '1.2'}
        spot_signed.return_value = {'orderId': 1}

        result = self.client.create_spot_order(payload)

        self.assertEqual(result, {'orderId': 1})
        spot_signed.assert_called_once_with('POST', '/api/v3/order', payload)
        self.assertEqual(spot_signed.call_args.args[2], payload)

    @patch('utils.fut_signed')
    def test_create_futures_order_reuses_utils_with_identical_payload(self, fut_signed):
        payload = {'symbol': 'SUIUSDT', 'side': 'SELL', 'type': 'MARKET', 'quantity': '31.5'}
        fut_signed.return_value = {'orderId': 2}

        result = self.client.create_futures_order(payload)

        self.assertEqual(result, {'orderId': 2})
        fut_signed.assert_called_once_with('POST', '/fapi/v1/order', payload)
        self.assertEqual(fut_signed.call_args.args[2], payload)

    @patch('utils.spot_signed')
    def test_create_oco_reuses_utils_with_identical_payload(self, spot_signed):
        payload = {'symbol': 'ETHUSDT', 'quantity': '1', 'price': '10', 'stopPrice': '9'}

        self.client.create_oco(payload)

        spot_signed.assert_called_once_with('POST', '/api/v3/order/oco', payload)

    @patch('utils.spot_signed')
    def test_transfer_reuses_utils_with_identical_payload(self, spot_signed):
        payload = {'type': 'MAIN_UMFUTURE', 'asset': 'USDT', 'amount': '10'}

        self.client.transfer(payload)

        spot_signed.assert_called_once_with('POST', '/sapi/v1/asset/transfer', payload)

    @patch('utils.spot_public')
    @patch('utils.fut_public')
    def test_exchange_info_routes_public_queries(self, fut_public, spot_public):
        self.client.exchange_info('spot', {'symbol': 'ETHUSDT'})
        self.client.exchange_info('futures', {'symbol': 'SUIUSDT'})

        spot_public.assert_called_once_with('/api/v3/exchangeInfo', {'symbol': 'ETHUSDT'})
        fut_public.assert_called_once_with('/fapi/v1/exchangeInfo', {'symbol': 'SUIUSDT'})

    @patch('utils.spot_public', return_value=[{'symbol': 'ETHUSDT', 'price': '1'}])
    def test_spot_ticker_prices_reuses_public_utils(self, spot_public):
        self.assertEqual(self.client.spot_ticker_prices(), [{'symbol': 'ETHUSDT', 'price': '1'}])
        spot_public.assert_called_once_with('/api/v3/ticker/price', None)

    @patch('utils.get_spot_price', return_value=1.0)
    @patch('utils.get_fut_price', return_value=2.0)
    def test_get_price_routes_market(self, get_fut_price, get_spot_price):
        self.assertEqual(self.client.get_price('ETHUSDT', 'spot'), 1.0)
        self.assertEqual(self.client.get_price('ETHUSDT', 'futures'), 2.0)
        get_spot_price.assert_called_once_with('ETHUSDT')
        get_fut_price.assert_called_once_with('ETHUSDT')

    @patch('utils.get_klines', return_value=[[1, 2, 3]])
    def test_get_klines_reuses_utils_with_identical_arguments(self, get_klines):
        result = self.client.get_klines('BTCUSDT', '4h', 60, True)

        self.assertEqual(result, [[1, 2, 3]])
        get_klines.assert_called_once_with('BTCUSDT', '4h', 60, True)

    @patch('utils.clean_dust', return_value=(['BNB'], 'ok'))
    def test_clean_dust_reuses_utils_with_identical_flag(self, clean_dust):
        self.assertEqual(self.client.clean_dust(dry_run=False), (['BNB'], 'ok'))
        clean_dust.assert_called_once_with(False)

    @patch('utils.fut_signed')
    def test_errors_propagate_unchanged(self, fut_signed):
        err = RuntimeError('same error')
        fut_signed.side_effect = err

        with self.assertRaises(RuntimeError) as ctx:
            self.client.create_futures_order({'symbol': 'ETHUSDT'})

        self.assertIs(ctx.exception, err)

    @patch('utils.get_spot_account', return_value={'balances': []})
    @patch('utils.fut_signed', return_value={'assets': []})
    @patch('utils.get_total_futures', return_value=12.5)
    @patch('utils.get_futures_summary', return_value=(12.5, 11.0, 1.5))
    def test_account_helpers_reuse_utils(self, get_futures_summary, get_total_futures, fut_signed, get_spot_account):
        self.assertEqual(self.client.spot_account(), {'balances': []})
        self.assertEqual(self.client.futures_account(), {'assets': []})
        self.assertEqual(self.client.get_total_futures(), 12.5)
        self.assertEqual(self.client.get_futures_summary(), (12.5, 11.0, 1.5))
        get_spot_account.assert_called_once_with()
        fut_signed.assert_called_once_with('GET', '/fapi/v2/account', {})
        get_total_futures.assert_called_once_with()
        get_futures_summary.assert_called_once_with()

    def test_default_client_can_be_injected(self):
        original = binance_client.get_default_client()
        fake = object()
        try:
            self.assertIs(binance_client.set_default_client(fake), fake)
            self.assertIs(binance_client.get_default_client(), fake)
        finally:
            binance_client.set_default_client(original)


if __name__ == '__main__':
    unittest.main()
