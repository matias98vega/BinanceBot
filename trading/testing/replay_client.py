"""Deterministic offline replay layered on the existing fake exchange engine."""
from copy import deepcopy

from .fake_binance_client import FakeBinanceClient, FakeBinanceError
from .fake_exchange_state import FakeExchangeState, dec
from .replay_events import thaw
from .replay_tape import ReplayCursor, ReplayTape


class ReplayClient(FakeBinanceClient):
    """Apply a versioned tape to FakeExchangeState without any network fallback."""

    replay_only = True
    network_allowed = False

    def __init__(self, tape):
        if not isinstance(tape, ReplayTape):
            tape = ReplayTape.from_dict(tape)
        self.tape = tape
        state = FakeExchangeState(epoch_ms=tape.started_at_ms, spot_balances={}, futures_wallet_balance=dec(0))
        super().__init__(state)
        self.cursor = ReplayCursor(tape)
        self.reconciliation_events = []
        self.pause_events = []
        self.operational_events = []
        self.applied_events = []
        self.action_results = []
        self._load_initial_state(thaw(tape.initial_state))

    @property
    def fidelity(self):
        return {'mode': self.tape.mode, 'complete': self.tape.complete,
                'missing_fields': list(self.tape.missing_fields), 'network_fallback': False}

    def _load_initial_state(self, initial):
        self.state.epoch_ms = int(initial.get('epoch_ms', self.tape.started_at_ms))
        self.state.fee_rate = dec(initial.get('fee_rate', self.state.fee_rate))
        balances = initial.get('balances', initial.get('spot_balances', {}))
        if isinstance(balances, list):
            balances = {row['asset']: {'free': row.get('free', 0), 'locked': row.get('locked', 0)} for row in balances}
        for asset, value in balances.items():
            if isinstance(value, dict): self.state.set_balance(asset, value.get('free', 0), value.get('locked', 0))
            else: self.state.set_balance(asset, value)
        for symbol, price in (initial.get('prices') or {}).items(): self.state.set_price(symbol, price)
        for row in initial.get('klines', []): self._apply_klines(row)
        for row in initial.get('filters', []): self.state.set_filters(row['symbol'], bool(row.get('futures')), **row['values'])
        self.state.futures_wallet_balance = dec(initial.get('futures_wallet_balance', self.state.futures_wallet_balance))
        positions = initial.get('futures_positions') or []
        if isinstance(positions, dict):
            positions = [{'symbol': symbol, **value} for symbol, value in positions.items()]
        for row in positions: self._apply_position(row)
        for row in initial.get('open_orders', initial.get('orders', [])):
            self.state.orders[int(row['orderId'])] = deepcopy(row)
        for row in initial.get('order_lists', []): self.state.order_lists[int(row['orderListId'])] = deepcopy(row)
        self.state.trades = deepcopy(initial.get('trades') or [])
        ids = initial.get('next_ids') or {}
        self.state.next_order_id = int(ids.get('order', max(self.state.orders, default=self.state.next_order_id - 1) + 1))
        self.state.next_list_id = int(ids.get('order_list', max(self.state.order_lists, default=self.state.next_list_id - 1) + 1))
        recorded_trade_ids = [int(row['id']) for row in self.state.trades if str(row.get('id', '')).isdigit()]
        self.state.next_trade_id = int(ids.get('trade', max(recorded_trade_ids, default=self.state.next_trade_id - 1) + 1))

    def _apply_klines(self, payload):
        key = (str(payload['symbol']).upper(), payload.get('interval', '1h'), bool(payload.get('futures')))
        self.state.klines[key] = deepcopy(payload.get('rows') or [])

    def _apply_position(self, payload):
        symbol = str(payload['symbol']).upper()
        amount = dec(payload.get('positionAmt', payload.get('quantity', 0)))
        if not amount:
            self.state.futures_positions.pop(symbol, None); return
        self.state.futures_positions[symbol] = {
            'positionAmt': amount, 'entryPrice': dec(payload.get('entryPrice', payload.get('entry_price', self.state.price(symbol)))),
            'leverage': int(payload.get('leverage', self.state.leverage.get(symbol, 1))),
        }

    def _apply_balance_rows(self, balances):
        for asset, value in balances.items():
            if isinstance(value, dict): self.state.set_balance(asset, value.get('free', 0), value.get('locked', 0))
            else: self.state.set_balance(asset, value)

    def _apply_fill_snapshot(self, payload):
        order = payload.get('order')
        trade = payload.get('trade')
        if order: self.state.orders[int(order['orderId'])] = deepcopy(order)
        if trade: self.state.trades.append(deepcopy(trade))
        if payload.get('balances'): self._apply_balance_rows(payload['balances'])
        if payload.get('futures_position'): self._apply_position(payload['futures_position'])
        if payload.get('futures_wallet_balance') is not None:
            self.state.futures_wallet_balance = dec(payload['futures_wallet_balance'])

    def _apply_event(self, event):
        payload, kind = thaw(event.payload), event.event_type
        result = None
        if kind == 'PRICE': self.state.set_price(payload['symbol'], payload['price'])
        elif kind == 'KLINES': self._apply_klines(payload)
        elif kind == 'BALANCE': self.state.set_balance(payload['asset'], payload.get('free', 0), payload.get('locked', 0))
        elif kind == 'FUTURES_WALLET': self.state.futures_wallet_balance = dec(payload['balance'])
        elif kind == 'FUTURES_POSITION': self._apply_position(payload)
        elif kind == 'SPOT_ORDER': result = self.create_spot_order(payload['params'])
        elif kind == 'FUTURES_ORDER': result = self.create_futures_order(payload['params'])
        elif kind == 'OCO_CREATE': result = self.create_oco(payload['params'])
        elif kind == 'OCO_TRIGGER': result = self.trigger_oco(payload['order_list_id'], payload.get('leg', 'tp'), payload.get('fill_ratio', 1))
        elif kind == 'ORDER_SNAPSHOT': self.state.orders[int(payload['orderId'])] = deepcopy(payload)
        elif kind == 'FILL_SNAPSHOT': self._apply_fill_snapshot(payload)
        elif kind == 'ERROR':
            error = FakeBinanceError(payload.get('message', 'replayed exchange error'), int(payload.get('code', -2010)), int(payload.get('status', 400)))
            self.state.queue_error(payload['operation'], error)
        elif kind == 'RECONCILIATION': self.reconciliation_events.append(deepcopy(payload))
        elif kind == 'PAUSE': self.pause_events.append(deepcopy(payload))
        elif kind == 'OPERATIONAL_EVENT': self.operational_events.append(deepcopy(payload))
        self.applied_events.append(event.as_dict())
        if result is not None: self.action_results.append({'event': event.as_dict(), 'result': deepcopy(result)})
        return result

    def advance_to(self, at_ms):
        events = self.cursor.pop_until(at_ms)
        for event in events:
            self.state.epoch_ms = event.at_ms
            self._apply_event(event)
        self.state.epoch_ms = int(at_ms)
        return events

    def advance(self, seconds=1):
        return self.advance_to(self.state.epoch_ms + int(dec(seconds) * 1000))

    def step(self):
        events = self.cursor.pop_next_batch()
        for event in events:
            self.state.epoch_ms = event.at_ms
            self._apply_event(event)
        return events

    def run_to_end(self):
        while not self.cursor.done: self.step()
        return self.snapshot()

    def snapshot(self):
        return {
            'scenario_id': self.tape.scenario_id, 'tape_fingerprint': self.tape.fingerprint,
            'cursor': {'index': self.cursor.index, 'at_ms': self.cursor.at_ms, 'done': self.cursor.done},
            'fidelity': self.fidelity, 'exchange': self.state.snapshot(),
            'reconciliations': deepcopy(self.reconciliation_events), 'pauses': deepcopy(self.pause_events),
            'operational_events': deepcopy(self.operational_events), 'applied_events': deepcopy(self.applied_events),
            'action_results': deepcopy(self.action_results),
        }
