"""Declarative reusable exchange scenarios A-L for integration tests."""
from dataclasses import dataclass

from .fake_binance_client import FakeBinanceClient, FakeBinanceError
from .fake_exchange_state import FakeExchangeState


@dataclass(frozen=True)
class Scenario:
    key: str
    description: str
    client: FakeBinanceClient


def _base(spot=100, futures=100):
    state = FakeExchangeState(futures_wallet_balance=futures)
    state.set_balance('USDT', spot)
    state.set_price('BTCUSDT', 100)
    state.set_filters('BTCUSDT', tick_size='.01', step_size='.001', min_qty='.001', min_notional='5')
    state.set_filters('BTCUSDT', futures=True, tick_size='.01', step_size='.001', min_qty='.001', min_notional='5')
    return FakeBinanceClient(state)


def build_scenario(key):
    key = str(key).upper()
    client = _base()
    descriptions = {
        'A': 'successful spot long with OCO',
        'B': 'spot market buy rejected',
        'C': 'OCO rejected after spot fill',
        'D': 'spot take-profit fill',
        'E': 'spot stop-loss fill',
        'F': 'external spot close / stale local state',
        'G': 'successful futures short with reduceOnly protection',
        'H': 'reduceOnly market close',
        'I': 'orphan futures short',
        'J': 'spot-to-futures rebalance',
        'K': 'circuit-breaker state with rejected write',
        'L': 'capacity boundary with existing positions',
    }
    if key not in descriptions:
        raise KeyError(f'Unknown fake scenario {key}')
    if key == 'B':
        client.state.queue_error('spot_signed:POST:/api/v3/order', FakeBinanceError('market buy rejected', -2010))
    elif key == 'C':
        client.state.queue_error('spot_signed:POST:/api/v3/order/oco', FakeBinanceError('OCO rejected', -2010))
    elif key in ('D', 'E'):
        client.create_spot_order({'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quantity': '.1'})
        oco = client.create_oco({'symbol': 'BTCUSDT', 'side': 'SELL', 'quantity': '.099', 'price': '110',
                                 'stopPrice': '90', 'stopLimitPrice': '89', 'stopLimitTimeInForce': 'GTC'})
        client.trigger_oco(oco['orderListId'], 'tp' if key == 'D' else 'sl')
    elif key == 'F':
        client.state.set_balance('BTC', 0)
    elif key in ('G', 'H', 'I'):
        client.set_futures_leverage('BTCUSDT', 2)
        client.create_futures_order({'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET', 'quantity': '.1'})
        if key == 'G':
            client.create_futures_order({'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'LIMIT', 'quantity': '.1',
                                         'price': '90', 'timeInForce': 'GTC', 'reduceOnly': 'true'})
        elif key == 'H':
            client.create_futures_order({'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quantity': '.1', 'reduceOnly': 'true'})
    elif key == 'J':
        client.transfer({'type': 'MAIN_UMFUTURE', 'asset': 'USDT', 'amount': '25'})
    elif key == 'K':
        client.state.queue_error('futures_signed:POST:/fapi/v1/order', FakeBinanceError('circuit breaker', -3000))
    elif key == 'L':
        client.set_futures_leverage('BTCUSDT', 2)
        client.create_futures_order({'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET', 'quantity': '.1'})
    return Scenario(key, descriptions[key], client)


def all_scenarios():
    return [build_scenario(chr(code)) for code in range(ord('A'), ord('L') + 1)]
