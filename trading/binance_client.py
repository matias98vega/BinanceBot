#!/usr/bin/env python3
"""Injectable Binance access layer backed by existing utils functions."""
import utils


class BinanceClient:
    """Thin adapter over utils. It must not change payloads, signing or retries."""

    def spot_public(self, path, params=None):
        return utils.spot_public(path, params)

    def futures_public(self, path, params=None):
        return utils.fut_public(path, params)

    def fut_public(self, path, params=None):
        return self.futures_public(path, params)

    def spot_signed(self, method, path, params=None):
        return utils.spot_signed(method, path, params)

    def futures_signed(self, method, path, params=None):
        return utils.fut_signed(method, path, params)

    def fut_signed(self, method, path, params=None):
        return self.futures_signed(method, path, params)

    def get_price(self, symbol, market='spot'):
        if str(market).lower() in ('futures', 'future', 'fut'):
            return utils.get_fut_price(symbol)
        return utils.get_spot_price(symbol)

    def get_spot_price(self, symbol):
        return utils.get_spot_price(symbol)

    def spot_ticker_prices(self):
        return self.spot_public('/api/v3/ticker/price')

    def get_futures_price(self, symbol):
        return utils.get_fut_price(symbol)

    def get_fut_price(self, symbol):
        return self.get_futures_price(symbol)

    def get_klines(self, symbol, interval='1h', limit=100, futures=False):
        return utils.get_klines(symbol, interval, limit, futures)

    def spot_account(self):
        return utils.get_spot_account()

    def get_spot_account(self):
        return self.spot_account()

    def futures_account(self):
        return self.futures_signed('GET', '/fapi/v2/account', {})

    def get_usdt_spot(self):
        return utils.get_usdt_spot()

    def get_usdt_futures(self):
        return utils.get_usdt_futures()

    def get_total_futures(self):
        return utils.get_total_futures()

    def get_futures_summary(self):
        return utils.get_futures_summary()

    def get_asset_spot(self, asset):
        return utils.get_asset_spot(asset)

    def exchange_info(self, market='spot', params=None):
        if str(market).lower() in ('futures', 'future', 'fut'):
            return self.futures_public('/fapi/v1/exchangeInfo', params)
        return self.spot_public('/api/v3/exchangeInfo', params)

    def get_spot_filters(self, symbol):
        return utils.get_spot_filters(symbol)

    def get_futures_filters(self, symbol):
        return utils.get_futures_filters(symbol)

    def create_spot_order(self, params):
        return self.spot_signed('POST', '/api/v3/order', params)

    def create_futures_order(self, params):
        return self.futures_signed('POST', '/fapi/v1/order', params)

    def create_oco(self, params):
        return self.spot_signed('POST', '/api/v3/order/oco', params)

    def get_spot_order(self, params):
        return self.spot_signed('GET', '/api/v3/order', params)

    def get_futures_order(self, params):
        return self.futures_signed('GET', '/fapi/v1/order', params)

    def get_order_list(self, params):
        return self.spot_signed('GET', '/api/v3/orderList', params)

    def cancel_spot_order(self, params):
        return self.spot_signed('DELETE', '/api/v3/order', params)

    def cancel_order_list(self, params):
        return self.spot_signed('DELETE', '/api/v3/orderList', params)

    def cancel_futures_order(self, params):
        return self.futures_signed('DELETE', '/fapi/v1/order', params)

    def futures_position_risk(self, params=None):
        return self.futures_signed('GET', '/fapi/v2/positionRisk', params or {})

    def futures_open_orders(self, params=None):
        return self.futures_signed('GET', '/fapi/v1/openOrders', params or {})

    def set_futures_leverage(self, symbol, leverage):
        return self.futures_signed('POST', '/fapi/v1/leverage', {'symbol': symbol, 'leverage': leverage})

    def transfer(self, params):
        return self.spot_signed('POST', '/sapi/v1/asset/transfer', params)

    def my_trades(self, params):
        return self.spot_signed('GET', '/api/v3/myTrades', params)

    def clean_dust(self, dry_run=True):
        return utils.clean_dust(dry_run)


_DEFAULT_CLIENT = BinanceClient()


def get_default_client():
    return _DEFAULT_CLIENT


def set_default_client(client):
    global _DEFAULT_CLIENT
    _DEFAULT_CLIENT = client
    return _DEFAULT_CLIENT
