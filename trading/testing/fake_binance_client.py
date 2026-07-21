"""Reusable test-only BinanceClient double with zero network capability."""
from copy import deepcopy
from decimal import Decimal, ROUND_DOWN

from .fake_exchange_state import FakeExchangeState, dec


class FakeBinanceError(RuntimeError):
    def __init__(self, message, code=-2010, status=400):
        super().__init__(message)
        self.code, self.status = code, status


class FakeBinanceClient:
    """Duck-typed subset of BinanceClient used by BinanceBot.

    It deliberately has no HTTP, socket, credential or production-state code.
    Unsupported endpoints fail closed with NotImplementedError.
    """

    def __init__(self, state=None):
        self.state = state or FakeExchangeState()

    @property
    def calls(self):
        return self.state.calls

    def _call(self, operation, **payload):
        self.state.calls.append({'at_ms': self.state.epoch_ms, 'operation': operation, 'payload': deepcopy(payload)})
        self.state.raise_queued(operation)

    def assert_called(self, operation, **contains):
        return any(c['operation'] == operation and all(c['payload'].get(k) == v for k, v in contains.items()) for c in self.calls)

    @staticmethod
    def _asset(symbol):
        return str(symbol).upper().removesuffix('USDT')

    def get_spot_price(self, symbol):
        self._call('get_spot_price', symbol=symbol)
        return float(self.state.price(symbol))

    def get_futures_price(self, symbol):
        self._call('get_futures_price', symbol=symbol)
        return float(self.state.price(symbol))

    get_fut_price = get_futures_price

    def get_price(self, symbol, market='spot'):
        return self.get_futures_price(symbol) if str(market).lower().startswith('fut') else self.get_spot_price(symbol)

    def spot_ticker_prices(self):
        self._call('spot_ticker_prices')
        return [{'symbol': symbol, 'price': str(price)} for symbol, price in sorted(self.state.prices.items())]

    def get_klines(self, symbol, interval='1h', limit=100, futures=False):
        self._call('get_klines', symbol=symbol, interval=interval, limit=limit, futures=futures)
        rows = self.state.klines.get((str(symbol).upper(), interval, bool(futures)))
        if rows is None:
            price = str(self.state.price(symbol))
            rows = [[self.state.epoch_ms, price, price, price, price, '1', self.state.epoch_ms + 1, price, 1, '1', price, '0']]
        return deepcopy(rows[-int(limit):])

    def get_spot_account(self):
        self._call('get_spot_account')
        return {'balances': [{'asset': asset, 'free': str(v['free']), 'locked': str(v['locked'])} for asset, v in sorted(self.state.spot_balances.items())]}

    spot_account = get_spot_account

    def get_usdt_spot(self):
        self._call('get_usdt_spot')
        return float(self.state.balance('USDT')['free'])

    def get_asset_spot(self, asset):
        self._call('get_asset_spot', asset=asset)
        return float(self.state.balance(asset)['free'])

    def futures_account(self):
        self._call('futures_account')
        positions = self._position_rows()
        upnl = sum(dec(row['unRealizedProfit']) for row in positions)
        margin = sum(abs(dec(row['positionAmt']) * dec(row['entryPrice'])) / dec(row['leverage']) for row in positions)
        available = self.state.futures_wallet_balance - margin
        return {'totalWalletBalance': str(self.state.futures_wallet_balance), 'totalUnrealizedProfit': str(upnl),
                'availableBalance': str(available), 'totalPositionInitialMargin': str(margin), 'positions': positions,
                'assets': [{'asset': 'USDT', 'walletBalance': str(self.state.futures_wallet_balance), 'availableBalance': str(available)}]}

    def get_usdt_futures(self):
        return float(self.futures_account()['availableBalance'])

    def get_total_futures(self):
        account = self.futures_account()
        return float(dec(account['totalWalletBalance']) + dec(account['totalUnrealizedProfit']))

    def get_futures_summary(self):
        account = self.futures_account()
        return (float(dec(account['totalWalletBalance']) + dec(account['totalUnrealizedProfit'])),
                float(account['availableBalance']), float(account['totalPositionInitialMargin']))

    def get_spot_filters(self, symbol):
        self._call('get_spot_filters', symbol=symbol)
        return self.state.filters(symbol).as_dict()

    def get_futures_filters(self, symbol):
        self._call('get_futures_filters', symbol=symbol)
        return self.state.filters(symbol, futures=True).as_dict()

    def exchange_info(self, market='spot', params=None):
        symbol = (params or {}).get('symbol')
        symbols = [symbol] if symbol else sorted(self.state.prices)
        futures = str(market).lower().startswith('fut')
        return {'symbols': [self._exchange_symbol(item, futures) for item in symbols]}

    def _exchange_symbol(self, symbol, futures):
        f = self.state.filters(symbol, futures)
        return {'symbol': symbol, 'filters': [
            {'filterType': 'PRICE_FILTER', 'tickSize': str(f.tick_size)},
            {'filterType': 'LOT_SIZE', 'stepSize': str(f.step_size), 'minQty': str(f.min_qty)},
            {'filterType': 'MIN_NOTIONAL', 'minNotional': str(f.min_notional), 'notional': str(f.min_notional)},
        ]}

    def _validate(self, symbol, quantity, futures=False):
        qty, price = dec(quantity), self.state.price(symbol)
        f = self.state.filters(symbol, futures)
        rounded = (qty / f.step_size).to_integral_value(rounding=ROUND_DOWN) * f.step_size
        if qty != rounded or qty < f.min_qty:
            raise FakeBinanceError('Filter failure: LOT_SIZE', -1013)
        if qty * price < f.min_notional:
            raise FakeBinanceError('Filter failure: MIN_NOTIONAL', -1013)
        return qty, price

    def _spot_market(self, params):
        symbol, side = params['symbol'].upper(), params['side'].upper()
        qty, price = self._validate(symbol, params['quantity'])
        asset, quote = self.state.balance(self._asset(symbol)), self.state.balance('USDT')
        notional, fee = qty * price, qty * self.state.fee_rate
        if side == 'BUY':
            if quote['free'] < notional:
                raise FakeBinanceError('Account has insufficient balance', -2010)
            quote['free'] -= notional
            asset['free'] += qty - fee
            commission_asset, commission = self._asset(symbol), fee
        elif side == 'SELL':
            if asset['free'] < qty:
                raise FakeBinanceError('Account has insufficient balance', -2010)
            asset['free'] -= qty
            quote_fee = notional * self.state.fee_rate
            quote['free'] += notional - quote_fee
            commission_asset, commission = 'USDT', quote_fee
        else:
            raise FakeBinanceError('Unsupported side', -1100)
        order_id = self.state.order_id()
        order = {'symbol': symbol, 'orderId': order_id, 'status': 'FILLED', 'type': 'MARKET', 'side': side,
                 'origQty': str(qty), 'executedQty': str(qty), 'cummulativeQuoteQty': str(notional),
                 'price': '0', 'time': self.state.epoch_ms,
                 'fills': [{'price': str(price), 'qty': str(qty), 'commission': str(commission), 'commissionAsset': commission_asset}]}
        self.state.orders[order_id] = order
        self.state.trades.append({'id': self.state.trade_id(), 'orderId': order_id, 'symbol': symbol, 'price': str(price),
                                  'qty': str(qty), 'quoteQty': str(notional), 'commission': str(commission),
                                  'commissionAsset': commission_asset, 'time': self.state.epoch_ms, 'isBuyer': side == 'BUY'})
        return deepcopy(order)

    def _create_oco(self, params):
        symbol, qty = params['symbol'].upper(), dec(params['quantity'])
        self._validate(symbol, qty)
        balance = self.state.balance(self._asset(symbol))
        if balance['free'] < qty:
            raise FakeBinanceError('Account has insufficient balance for requested action', -2010)
        balance['free'] -= qty
        balance['locked'] += qty
        list_id, limit_id, stop_id = self.state.list_id(), self.state.order_id(), self.state.order_id()
        orders = []
        for oid, typ, price in ((limit_id, 'LIMIT_MAKER', params['price']), (stop_id, 'STOP_LOSS_LIMIT', params['stopLimitPrice'])):
            order = {'symbol': symbol, 'orderId': oid, 'orderListId': list_id, 'status': 'NEW', 'side': 'SELL',
                     'type': typ, 'price': str(price), 'stopPrice': str(params.get('stopPrice', 0)),
                     'origQty': str(qty), 'executedQty': '0', 'cummulativeQuoteQty': '0', 'time': self.state.epoch_ms}
            self.state.orders[oid] = order
            orders.append({'symbol': symbol, 'orderId': oid})
        self.state.order_lists[list_id] = {'orderListId': list_id, 'contingencyType': 'OCO', 'listOrderStatus': 'EXECUTING', 'orders': orders}
        return deepcopy(self.state.order_lists[list_id])

    def trigger_oco(self, order_list_id, leg='tp', fill_ratio=1):
        group = self.state.order_lists[int(order_list_id)]
        chosen = group['orders'][0 if str(leg).lower() == 'tp' else 1]['orderId']
        order = self.state.orders[chosen]
        qty = dec(order['origQty']) * dec(fill_ratio)
        price = dec(order['price'])
        balance = self.state.balance(self._asset(order['symbol']))
        balance['locked'] -= qty
        balance['free'] += dec(order['origQty']) - qty
        self.state.balance('USDT')['free'] += qty * price * (Decimal('1') - self.state.fee_rate)
        order.update(status='FILLED' if dec(fill_ratio) == 1 else 'PARTIALLY_FILLED', executedQty=str(qty), cummulativeQuoteQty=str(qty * price))
        for item in group['orders']:
            if item['orderId'] != chosen:
                self.state.orders[item['orderId']]['status'] = 'CANCELED'
        group['listOrderStatus'] = 'ALL_DONE' if dec(fill_ratio) == 1 else 'EXECUTING'
        return deepcopy(order)

    def _position_rows(self, symbol=None):
        rows = []
        for sym, pos in sorted(self.state.futures_positions.items()):
            if symbol and sym != str(symbol).upper():
                continue
            mark = self.state.price(sym)
            amt, entry = pos['positionAmt'], pos['entryPrice']
            upnl = (mark - entry) * amt
            rows.append({'symbol': sym, 'positionAmt': str(amt), 'entryPrice': str(entry), 'markPrice': str(mark),
                         'unRealizedProfit': str(upnl), 'leverage': str(pos['leverage']), 'updateTime': self.state.epoch_ms})
        return rows

    def _futures_order(self, params):
        symbol, side, typ = params['symbol'].upper(), params['side'].upper(), params['type'].upper()
        qty, price = self._validate(symbol, params['quantity'], futures=True)
        reduce_only = str(params.get('reduceOnly', 'false')).lower() == 'true'
        current = self.state.futures_positions.get(symbol, {'positionAmt': dec(0), 'entryPrice': price, 'leverage': self.state.leverage.get(symbol, 1)})
        delta = qty if side == 'BUY' else -qty
        if reduce_only and (current['positionAmt'] == 0 or current['positionAmt'] * delta > 0):
            raise FakeBinanceError('ReduceOnly Order is rejected', -2022)
        oid = self.state.order_id()
        order = {'symbol': symbol, 'orderId': oid, 'status': 'NEW', 'type': typ, 'side': side, 'origQty': str(qty),
                 'executedQty': '0', 'avgPrice': '0', 'price': str(params.get('price', 0)),
                 'stopPrice': str(params.get('stopPrice', 0)), 'reduceOnly': reduce_only, 'time': self.state.epoch_ms}
        if typ == 'MARKET':
            old_amt, new_amt = current['positionAmt'], current['positionAmt'] + delta
            if reduce_only and abs(delta) > abs(old_amt):
                new_amt = dec(0)
            if old_amt and old_amt * delta < 0:
                closed = min(abs(old_amt), abs(delta))
                realized = (price - current['entryPrice']) * closed * (Decimal('1') if old_amt > 0 else Decimal('-1'))
                self.state.futures_wallet_balance += realized - qty * price * self.state.fee_rate
            else:
                self.state.futures_wallet_balance -= qty * price * self.state.fee_rate
            if new_amt:
                current.update(positionAmt=new_amt, entryPrice=price if old_amt == 0 else current['entryPrice'], leverage=self.state.leverage.get(symbol, current['leverage']))
                self.state.futures_positions[symbol] = current
            else:
                self.state.futures_positions.pop(symbol, None)
            order.update(status='FILLED', executedQty=str(qty), avgPrice=str(price))
        self.state.orders[oid] = order
        return deepcopy(order)

    def create_spot_order(self, params): return self.spot_signed('POST', '/api/v3/order', params)
    def create_futures_order(self, params): return self.futures_signed('POST', '/fapi/v1/order', params)
    def create_oco(self, params): return self.spot_signed('POST', '/api/v3/order/oco', params)
    def get_spot_order(self, params): return self.spot_signed('GET', '/api/v3/order', params)
    def get_futures_order(self, params): return self.futures_signed('GET', '/fapi/v1/order', params)
    def get_order_list(self, params): return self.spot_signed('GET', '/api/v3/orderList', params)
    def cancel_spot_order(self, params): return self.spot_signed('DELETE', '/api/v3/order', params)
    def cancel_order_list(self, params): return self.spot_signed('DELETE', '/api/v3/orderList', params)
    def cancel_futures_order(self, params): return self.futures_signed('DELETE', '/fapi/v1/order', params)
    def futures_position_risk(self, params=None): return self.futures_signed('GET', '/fapi/v2/positionRisk', params or {})
    def futures_open_orders(self, params=None): return self.futures_signed('GET', '/fapi/v1/openOrders', params or {})
    def spot_open_orders(self, params=None): return self.spot_signed('GET', '/api/v3/openOrders', params or {})
    def set_futures_leverage(self, symbol, leverage): return self.futures_signed('POST', '/fapi/v1/leverage', {'symbol': symbol, 'leverage': leverage})
    def transfer(self, params): return self.spot_signed('POST', '/sapi/v1/asset/transfer', params)
    def my_trades(self, params): return self.spot_signed('GET', '/api/v3/myTrades', params)

    def spot_public(self, path, params=None):
        self._call(f'spot_public:{path}', params=params or {})
        if path == '/api/v3/ticker/price': return self.spot_ticker_prices()
        if path == '/api/v3/exchangeInfo': return self.exchange_info('spot', params)
        if path == '/api/v3/klines': return self.get_klines(**(params or {}))
        raise NotImplementedError(f'Fake spot public endpoint: {path}')

    def futures_public(self, path, params=None):
        self._call(f'futures_public:{path}', params=params or {})
        if path == '/fapi/v1/ticker/price':
            symbol = (params or {}).get('symbol')
            return {'symbol': symbol, 'price': str(self.state.price(symbol))}
        if path == '/fapi/v1/exchangeInfo': return self.exchange_info('futures', params)
        raise NotImplementedError(f'Fake futures public endpoint: {path}')
    fut_public = futures_public

    def spot_signed(self, method, path, params=None):
        params, method = deepcopy(params or {}), method.upper()
        op = f'spot_signed:{method}:{path}'
        self._call(op, params=params)
        if (method, path) == ('POST', '/api/v3/order'): return self._spot_market(params)
        if (method, path) == ('POST', '/api/v3/order/oco'): return self._create_oco(params)
        if (method, path) == ('GET', '/api/v3/account'): return self.get_spot_account()
        if (method, path) == ('GET', '/api/v3/order'): return deepcopy(self.state.orders[int(params['orderId'])])
        if (method, path) == ('GET', '/api/v3/orderList'): return deepcopy(self.state.order_lists[int(params['orderListId'])])
        if (method, path) == ('GET', '/api/v3/openOrders'):
            return [deepcopy(o) for o in self.state.orders.values() if o['status'] in ('NEW', 'PARTIALLY_FILLED') and (not params.get('symbol') or o['symbol'] == params['symbol'])]
        if (method, path) == ('GET', '/api/v3/myTrades'):
            return [deepcopy(t) for t in self.state.trades if t['symbol'] == params['symbol']][-int(params.get('limit', 500)):]
        if method == 'DELETE' and path == '/api/v3/orderList': return self._cancel_oco(params)
        if method == 'DELETE' and path == '/api/v3/order': return self._cancel_order(params)
        if (method, path) == ('POST', '/sapi/v1/asset/transfer'): return self._transfer(params)
        raise NotImplementedError(f'Fake spot signed endpoint: {method} {path}')

    def _cancel_order(self, params):
        order = self.state.orders[int(params['orderId'])]
        order['status'] = 'CANCELED'
        return deepcopy(order)

    def _cancel_oco(self, params):
        group = self.state.order_lists[int(params['orderListId'])]
        for item in group['orders']:
            self.state.orders[item['orderId']]['status'] = 'CANCELED'
        first = self.state.orders[group['orders'][0]['orderId']]
        qty = dec(first['origQty']) - dec(first['executedQty'])
        balance = self.state.balance(self._asset(first['symbol']))
        balance['locked'] -= qty; balance['free'] += qty
        group['listOrderStatus'] = 'ALL_DONE'
        return deepcopy(group)

    def _transfer(self, params):
        amount, kind = dec(params['amount']), params['type']
        spot = self.state.balance('USDT')
        if kind == 'MAIN_UMFUTURE':
            if spot['free'] < amount: raise FakeBinanceError('insufficient balance', -2010)
            spot['free'] -= amount; self.state.futures_wallet_balance += amount
        elif kind == 'UMFUTURE_MAIN':
            if dec(self.get_usdt_futures()) < amount: raise FakeBinanceError('insufficient balance', -2010)
            self.state.futures_wallet_balance -= amount; spot['free'] += amount
        else: raise FakeBinanceError('unsupported transfer type', -1100)
        event = {'tranId': len(self.state.transfers) + 1, **params}
        self.state.transfers.append(event)
        return deepcopy(event)

    def futures_signed(self, method, path, params=None):
        params, method = deepcopy(params or {}), method.upper()
        op = f'futures_signed:{method}:{path}'
        self._call(op, params=params)
        if (method, path) == ('GET', '/fapi/v2/account'): return self.futures_account()
        if (method, path) == ('GET', '/fapi/v2/positionRisk'): return self._position_rows(params.get('symbol'))
        if (method, path) == ('GET', '/fapi/v1/openOrders'):
            return [deepcopy(o) for o in self.state.orders.values() if o.get('reduceOnly') is not None and o['status'] in ('NEW', 'PARTIALLY_FILLED') and (not params.get('symbol') or o['symbol'] == params['symbol'])]
        if (method, path) == ('GET', '/fapi/v1/order'): return deepcopy(self.state.orders[int(params['orderId'])])
        if (method, path) == ('POST', '/fapi/v1/order'): return self._futures_order(params)
        if (method, path) == ('DELETE', '/fapi/v1/order'): return self._cancel_order(params)
        if (method, path) == ('POST', '/fapi/v1/leverage'):
            self.state.leverage[params['symbol'].upper()] = int(params['leverage'])
            return {'symbol': params['symbol'].upper(), 'leverage': int(params['leverage'])}
        raise NotImplementedError(f'Fake futures signed endpoint: {method} {path}')
    fut_signed = futures_signed

    def clean_dust(self, dry_run=True):
        self._call('clean_dust', dry_run=dry_run)
        if not dry_run: raise NotImplementedError('Fake dust conversion intentionally unsupported')
        return [], 'DRY-RUN: no fake dust'
