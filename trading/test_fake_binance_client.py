#!/usr/bin/env python3
import socket
import os
import sys
import unittest
from decimal import Decimal
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

from testing import FakeBinanceClient, FakeBinanceError, FakeExchangeState


class FakeBinanceClientTests(unittest.TestCase):
    def setUp(self):
        self.state = FakeExchangeState(fee_rate=Decimal('.001'))
        self.state.set_balance('USDT', 100)
        self.state.set_price('BTCUSDT', 100)
        self.state.set_filters('BTCUSDT', tick_size='.1', step_size='.01', min_qty='.01', min_notional='5')
        self.state.set_filters('BTCUSDT', futures=True, tick_size='.1', step_size='.01', min_qty='.01', min_notional='5')
        self.client = FakeBinanceClient(self.state)

    def test_balances_free_locked_and_snapshot_are_isolated(self):
        self.state.set_balance('BTC', 1, .25)
        account = self.client.get_spot_account()
        self.assertEqual(next(x for x in account['balances'] if x['asset'] == 'BTC')['locked'], '0.25')
        snapshot = self.state.snapshot()
        snapshot['spot_balances']['BTC']['free'] = 99
        self.assertEqual(self.state.balance('BTC')['free'], Decimal('1'))

    def test_market_buy_charges_asset_fee_once(self):
        order = self.client.create_spot_order({'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quantity': '.10'})
        self.assertEqual(order['status'], 'FILLED')
        self.assertEqual(self.state.balance('USDT')['free'], Decimal('90.00'))
        self.assertEqual(self.state.balance('BTC')['free'], Decimal('.09990'))
        self.assertEqual(order['fills'][0]['commissionAsset'], 'BTC')

    def test_market_sell_charges_quote_fee(self):
        self.state.set_balance('BTC', 1)
        order = self.client.create_spot_order({'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET', 'quantity': '.10'})
        self.assertEqual(self.state.balance('BTC')['free'], Decimal('.90'))
        self.assertEqual(self.state.balance('USDT')['free'], Decimal('109.99000'))
        self.assertEqual(order['fills'][0]['commissionAsset'], 'USDT')

    def test_filters_precision_and_min_notional(self):
        self.assertEqual(self.client.get_spot_filters('BTCUSDT')['tick_size'], .1)
        with self.assertRaisesRegex(FakeBinanceError, 'LOT_SIZE'):
            self.client.create_spot_order({'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quantity': '.015'})
        with self.assertRaisesRegex(FakeBinanceError, 'MIN_NOTIONAL'):
            self.client.create_spot_order({'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quantity': '.01'})

    def test_oco_locks_cancels_and_releases_balance(self):
        self.state.set_balance('BTC', 1)
        oco = self.client.create_oco({'symbol': 'BTCUSDT', 'side': 'SELL', 'quantity': '.10', 'price': '110',
                                      'stopPrice': '90', 'stopLimitPrice': '89', 'stopLimitTimeInForce': 'GTC'})
        self.assertEqual(self.state.balance('BTC')['locked'], Decimal('.10'))
        self.client.cancel_order_list({'orderListId': oco['orderListId'], 'symbol': 'BTCUSDT'})
        self.assertEqual(self.state.balance('BTC')['free'], Decimal('1.00'))
        self.assertEqual(self.client.get_order_list({'orderListId': oco['orderListId']})['listOrderStatus'], 'ALL_DONE')

    def test_oco_tp_sl_and_partial_fill(self):
        for leg, expected in (('tp', '110'), ('sl', '89')):
            state = FakeExchangeState(); state.set_balance('BTC', 1); state.set_price('BTCUSDT', 100)
            client = FakeBinanceClient(state)
            oco = client.create_oco({'symbol': 'BTCUSDT', 'side': 'SELL', 'quantity': '.1', 'price': '110',
                                     'stopPrice': '90', 'stopLimitPrice': '89', 'stopLimitTimeInForce': 'GTC'})
            filled = client.trigger_oco(oco['orderListId'], leg, fill_ratio='.5')
            self.assertEqual(filled['status'], 'PARTIALLY_FILLED')
            self.assertEqual(filled['price'], expected)

    def test_futures_open_upl_leverage_and_reduce_only_close(self):
        self.client.set_futures_leverage('BTCUSDT', 5)
        opened = self.client.create_futures_order({'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET', 'quantity': '.10'})
        self.assertEqual(opened['status'], 'FILLED')
        self.state.set_price('BTCUSDT', 90)
        row = self.client.futures_position_risk({'symbol': 'BTCUSDT'})[0]
        self.assertEqual(Decimal(row['unRealizedProfit']), Decimal('1.00'))
        self.client.create_futures_order({'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quantity': '.10', 'reduceOnly': 'true'})
        self.assertEqual(self.client.futures_position_risk({'symbol': 'BTCUSDT'}), [])

    def test_reduce_only_cannot_increase_exposure(self):
        with self.assertRaisesRegex(FakeBinanceError, 'ReduceOnly'):
            self.client.create_futures_order({'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quantity': '.10', 'reduceOnly': 'true'})

    def test_deterministic_ids_clock_calls_and_queued_exception(self):
        self.state.queue_error('get_spot_price', RuntimeError('queued'))
        with self.assertRaisesRegex(RuntimeError, 'queued'):
            self.client.get_spot_price('BTCUSDT')
        self.assertEqual(self.state.advance(2), 1_700_000_002_000)
        first = self.client.create_spot_order({'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quantity': '.10'})
        self.assertEqual(first['orderId'], 1000)
        self.assertTrue(self.client.assert_called('spot_signed:POST:/api/v3/order'))

    def test_transfer_updates_wallets_and_is_recorded(self):
        response = self.client.transfer({'type': 'MAIN_UMFUTURE', 'asset': 'USDT', 'amount': '25'})
        self.assertEqual(response['tranId'], 1)
        self.assertEqual(self.state.balance('USDT')['free'], Decimal('75'))
        self.assertEqual(self.state.futures_wallet_balance, Decimal('1025'))

    def test_no_network_even_when_socket_is_blocked(self):
        with patch.object(socket, 'socket', side_effect=AssertionError('network attempted')), \
             patch.object(socket, 'getaddrinfo', side_effect=AssertionError('DNS attempted')):
            self.assertEqual(self.client.get_spot_price('BTCUSDT'), 100)
            self.client.get_spot_account()

    def test_unsupported_endpoint_fails_closed(self):
        with self.assertRaises(NotImplementedError):
            self.client.spot_signed('POST', '/api/v3/something-real', {})


if __name__ == '__main__':
    unittest.main()
