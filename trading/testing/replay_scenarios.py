"""Small deterministic tapes; these are replay fixtures, not backtests."""
from datetime import datetime, timezone

from .replay_tape import ReplayTape


STARTED_AT = '2023-11-14T22:13:20Z'
START_MS = 1_700_000_000_000


def _base_initial():
    return {
        'balances': {'USDT': {'free': '100', 'locked': '0'}}, 'futures_wallet_balance': '100',
        'prices': {'BTCUSDT': '100'},
        'filters': [
            {'symbol': 'BTCUSDT', 'values': {'tick_size': '.01', 'step_size': '.001', 'min_qty': '.001', 'min_notional': '5'}},
            {'symbol': 'BTCUSDT', 'futures': True, 'values': {'tick_size': '.01', 'step_size': '.001', 'min_qty': '.001', 'min_notional': '5'}},
        ],
    }


def fixture_spot_long_tape():
    return ReplayTape.from_dict({
        'replay_schema_version': 1, 'scenario_id': 'fixture-spot-long-tp', 'mode': 'FIXTURE_REPLAY',
        'description': 'Spot buy, OCO protection and deterministic take-profit fill',
        'timezone': 'UTC', 'started_at': STARTED_AT, 'initial_state': _base_initial(),
        'events': [
            {'at_ms': START_MS + 1_000, 'event_type': 'PRICE', 'payload': {'symbol': 'BTCUSDT', 'price': '100'}},
            {'at_ms': START_MS + 2_000, 'event_type': 'SPOT_ORDER', 'payload': {'params': {'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quantity': '.1'}}},
            {'at_ms': START_MS + 3_000, 'event_type': 'OCO_CREATE', 'payload': {'params': {'symbol': 'BTCUSDT', 'side': 'SELL', 'quantity': '.099', 'price': '110', 'stopPrice': '90', 'stopLimitPrice': '89', 'stopLimitTimeInForce': 'GTC'}}},
            {'at_ms': START_MS + 4_000, 'event_type': 'PRICE', 'payload': {'symbol': 'BTCUSDT', 'price': '110'}},
            {'at_ms': START_MS + 5_000, 'event_type': 'OCO_TRIGGER', 'payload': {'order_list_id': 500, 'leg': 'tp'}},
            {'at_ms': START_MS + 5_000, 'event_type': 'OPERATIONAL_EVENT', 'payload': {'event_type': 'position_reconciled', 'state': 'RUNNING', 'reason_code': 'TP_FILLED'}},
        ],
    })


def fixture_futures_error_tape():
    return ReplayTape.from_dict({
        'replay_schema_version': 1, 'scenario_id': 'fixture-futures-error-recovery', 'mode': 'FIXTURE_REPLAY',
        'description': 'Queued exchange error, pause evidence and later reconciliation',
        'timezone': 'UTC', 'started_at': STARTED_AT, 'initial_state': _base_initial(),
        'events': [
            {'at_ms': START_MS + 1_000, 'event_type': 'ERROR', 'payload': {'operation': 'futures_signed:POST:/fapi/v1/order', 'message': 'fixture timeout', 'code': -1007, 'status': 504}},
            {'at_ms': START_MS + 1_000, 'event_type': 'PAUSE', 'payload': {'state': 'PAUSED_RISK', 'reason_code': 'EXCHANGE_TIMEOUT'}},
            {'at_ms': START_MS + 2_000, 'event_type': 'RECONCILIATION', 'payload': {'status': 'ALIGNED', 'reason_code': 'RECOVERED'}},
            {'at_ms': START_MS + 2_000, 'event_type': 'OPERATIONAL_EVENT', 'payload': {'event_type': 'trading_resumed', 'state': 'RUNNING'}},
        ],
    })


def recorded_observation_tape(scenario_id, started_at, initial_state, events, missing_fields):
    return ReplayTape.from_dict({
        'replay_schema_version': 1, 'scenario_id': scenario_id, 'mode': 'RECORDED_OBSERVATION_REPLAY',
        'timezone': 'UTC', 'started_at': started_at, 'initial_state': initial_state,
        'events': events, 'missing_fields': list(missing_fields),
        'metadata': {'limitations': 'Only explicitly recorded exchange observations are reproduced.'},
    })


def historical_event_tape(scenario_id, rows):
    """Map existing history to analysis-only operational events; never invent exchange responses."""
    rows = list(rows)
    if not rows: raise ValueError('historical replay requires at least one event')
    def stamp(row):
        value = row.get('recorded_at') or row.get('timestamp') or row.get('opened_at') or row.get('closed_at')
        return int(datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp() * 1000)
    ordered = sorted(rows, key=stamp)
    started = datetime.fromtimestamp(stamp(ordered[0]) / 1000, timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    events = [{'at_ms': stamp(row), 'event_type': 'OPERATIONAL_EVENT', 'sequence': index,
               'payload': {'historical_record': row, 'event_type': row.get('event_type') or row.get('event')}}
              for index, row in enumerate(ordered)]
    return ReplayTape.from_dict({
        'replay_schema_version': 1, 'scenario_id': scenario_id, 'mode': 'HISTORICAL_EVENT_REPLAY',
        'timezone': 'UTC', 'started_at': started, 'initial_state': {}, 'events': events,
        'missing_fields': ['balances', 'exchange_responses', 'fills', 'klines', 'open_orders', 'positions'],
        'metadata': {'analysis_only': True, 'exchange_equivalence': False},
    })


SCENARIOS = {'spot-long-tp': fixture_spot_long_tape, 'futures-error-recovery': fixture_futures_error_tape}


def build_replay_scenario(name):
    try: return SCENARIOS[str(name)]()
    except KeyError: raise KeyError(f'unknown replay scenario: {name}') from None
