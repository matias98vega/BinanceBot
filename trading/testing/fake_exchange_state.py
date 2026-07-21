"""In-memory exchange state for tests only.

This module has no imports from the production Binance transport and performs no
I/O.  Monetary values use Decimal internally so scenario results are stable.
"""
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal


def dec(value):
    return Decimal(str(value))


@dataclass(frozen=True)
class SymbolFilters:
    tick_size: Decimal = Decimal('0.01')
    step_size: Decimal = Decimal('0.001')
    min_qty: Decimal = Decimal('0.001')
    min_notional: Decimal = Decimal('5')

    def as_dict(self):
        return {key: float(value) for key, value in vars(self).items()}


@dataclass
class FakeExchangeState:
    """Complete mutable state owned by one fake client instance."""

    epoch_ms: int = 1_700_000_000_000
    fee_rate: Decimal = Decimal('0.001')
    spot_balances: dict = field(default_factory=lambda: {'USDT': {'free': Decimal('1000'), 'locked': Decimal('0')}})
    futures_wallet_balance: Decimal = Decimal('1000')
    prices: dict = field(default_factory=dict)
    klines: dict = field(default_factory=dict)
    spot_filters: dict = field(default_factory=dict)
    futures_filters: dict = field(default_factory=dict)
    orders: dict = field(default_factory=dict)
    order_lists: dict = field(default_factory=dict)
    futures_positions: dict = field(default_factory=dict)
    leverage: dict = field(default_factory=dict)
    trades: list = field(default_factory=list)
    transfers: list = field(default_factory=list)
    calls: list = field(default_factory=list)
    queued_errors: dict = field(default_factory=dict)
    next_order_id: int = 1000
    next_list_id: int = 500
    next_trade_id: int = 1

    def set_balance(self, asset, free, locked=0):
        self.spot_balances[str(asset).upper()] = {'free': dec(free), 'locked': dec(locked)}

    def balance(self, asset):
        return self.spot_balances.setdefault(str(asset).upper(), {'free': dec(0), 'locked': dec(0)})

    def set_price(self, symbol, price):
        self.prices[str(symbol).upper()] = dec(price)

    def price(self, symbol):
        symbol = str(symbol).upper()
        if symbol not in self.prices:
            raise KeyError(f'No fake price configured for {symbol}')
        return self.prices[symbol]

    def filters(self, symbol, futures=False):
        table = self.futures_filters if futures else self.spot_filters
        return table.get(str(symbol).upper(), SymbolFilters())

    def set_filters(self, symbol, futures=False, **values):
        table = self.futures_filters if futures else self.spot_filters
        table[str(symbol).upper()] = SymbolFilters(**{key: dec(value) for key, value in values.items()})

    def advance(self, seconds=1):
        self.epoch_ms += int(dec(seconds) * 1000)
        return self.epoch_ms

    def order_id(self):
        value = self.next_order_id
        self.next_order_id += 1
        return value

    def list_id(self):
        value = self.next_list_id
        self.next_list_id += 1
        return value

    def trade_id(self):
        value = self.next_trade_id
        self.next_trade_id += 1
        return value

    def queue_error(self, operation, error):
        self.queued_errors.setdefault(operation, []).append(error)

    def raise_queued(self, operation):
        queue = self.queued_errors.get(operation) or []
        if queue:
            raise queue.pop(0)

    def snapshot(self):
        return deepcopy({
            'epoch_ms': self.epoch_ms, 'spot_balances': self.spot_balances,
            'futures_wallet_balance': self.futures_wallet_balance, 'prices': self.prices,
            'orders': self.orders, 'order_lists': self.order_lists,
            'futures_positions': self.futures_positions, 'leverage': self.leverage,
            'trades': self.trades, 'transfers': self.transfers, 'calls': self.calls,
        })
