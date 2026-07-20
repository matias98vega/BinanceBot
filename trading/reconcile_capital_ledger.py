#!/usr/bin/env python3
"""Safe bootstrap planner/applicator for the capital ledger.

The apply path is explicit, re-observes balances immediately before writing,
and never performs orders or transfers. This task does not run --apply.
"""
import argparse
import hashlib
import json
import math
import os
from datetime import datetime, timezone

import binance_client
import capital_ledger
import version_history
from config_loader import PROJECT_DIR

STATE_FILE = os.path.join(PROJECT_DIR, 'trading', 'state.json')
BOT_STATE_FILE = os.path.join(PROJECT_DIR, 'trading', 'bot_state.json')
REBALANCE_STATUS_FILE = os.path.join(PROJECT_DIR, 'data', 'history', 'rebalance_status.json')
ABS_TOLERANCE_USDT = 0.20
PCT_TOLERANCE = 0.001
MAX_AGE_SECONDS = 300
PLAN_NAME = 'capital-ledger-bootstrap'
CONVENTION = 'realized_pnl_net_of_fees_plus_signed_funding'


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(dt):
    return dt.isoformat().replace('+00:00', 'Z')


def _read_json(path):
    try:
        with open(path, encoding='utf-8') as file:
            value = json.load(file)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _ticker_map(client):
    result = {}
    for row in client.spot_ticker_prices() or []:
        if isinstance(row, dict) and row.get('symbol') and _number(row.get('price')) is not None:
            result[row['symbol']] = float(row['price'])
    return result


def observe_capital(client=None, now=None):
    client = client or binance_client.get_default_client()
    observed_at = now or _now()
    state = _read_json(STATE_FILE)
    bot_state = _read_json(BOT_STATE_FILE)
    rebalance_status = _read_json(REBALANCE_STATUS_FILE)
    errors = []
    spot_account = client.get_spot_account()
    futures_account = client.futures_account()
    prices = _ticker_map(client)
    balances = {}
    spot_equity = 0.0
    for row in spot_account.get('balances', []) if isinstance(spot_account, dict) else []:
        asset = str(row.get('asset') or '')
        quantity = (_number(row.get('free')) or 0.0) + (_number(row.get('locked')) or 0.0)
        balances[asset] = quantity
        if quantity <= 0:
            continue
        if asset == 'USDT':
            spot_equity += quantity
        else:
            price = prices.get(f'{asset}USDT')
            if price is None:
                errors.append(f'missing_current_price:{asset}USDT')
            else:
                spot_equity += quantity * price
    open_positions = []
    spot_upnl = 0.0
    for position in state.get('positions', []) if isinstance(state.get('positions'), list) else []:
        if not isinstance(position, dict):
            continue
        direction = str(position.get('direction') or '').lower()
        symbol = str(position.get('symbol') or '')
        quantity = _number(position.get('quantity'))
        entry = _number(position.get('entry_price'))
        if quantity is None:
            errors.append(f'missing_quantity:{symbol}')
            continue
        if entry is None:
            errors.append(f'missing_entry_price:{symbol}')
            continue
        if direction == 'long':
            asset = symbol[:-4] if symbol.endswith('USDT') else symbol
            observed_quantity = balances.get(asset)
            if observed_quantity is None or observed_quantity + 1e-8 < quantity:
                errors.append(f'managed_spot_quantity_mismatch:{symbol}:state={quantity}:binance={observed_quantity}')
                continue
            price = prices.get(symbol)
            if price is None:
                errors.append(f'missing_current_price:{symbol}')
                continue
            upnl = (price - entry) * quantity
            spot_upnl += upnl
            open_positions.append({'symbol': symbol, 'wallet': 'SPOT', 'side': 'LONG', 'quantity': quantity, 'entry_price': entry, 'current_price': price, 'unrealized_pnl': round(upnl, 8)})
    futures_upnl = _number(futures_account.get('totalUnrealizedProfit')) if isinstance(futures_account, dict) else None
    futures_wallet = _number(futures_account.get('totalWalletBalance')) if isinstance(futures_account, dict) else None
    if futures_upnl is None:
        errors.append('missing_futures_unrealized_pnl')
    if futures_wallet is None:
        errors.append('missing_futures_wallet_balance')
    for position in futures_account.get('positions', []) if isinstance(futures_account, dict) else []:
        amount = _number(position.get('positionAmt'))
        if amount is None or abs(amount) <= 0:
            continue
        entry = _number(position.get('entryPrice'))
        mark = _number(position.get('markPrice'))
        upnl = _number(position.get('unrealizedProfit'))
        missing = [name for name, value in [('entry_price', entry), ('current_price', mark), ('unrealized_pnl', upnl)] if value is None]
        if missing:
            errors.append(f'missing_futures_position_fields:{position.get("symbol")}:{",".join(missing)}')
            continue
        open_positions.append({'symbol': position.get('symbol'), 'wallet': 'FUTURES', 'side': 'LONG' if amount > 0 else 'SHORT', 'quantity': abs(amount), 'entry_price': entry, 'current_price': mark, 'unrealized_pnl': upnl})
    futures_real = None if futures_wallet is None or futures_upnl is None else futures_wallet + futures_upnl
    equity = None if futures_real is None else spot_equity + futures_real
    baseline = None if futures_upnl is None or errors else spot_upnl + futures_upnl
    transfer_active = bool(rebalance_status.get('transfer_in_progress') or str(rebalance_status.get('status') or '').upper() in {'IN_PROGRESS', 'ATTEMPT'})
    if transfer_active:
        errors.append('transfer_in_progress')
    if rebalance_status.get('critical_error_active') or rebalance_status.get('critical_error'):
        errors.append('critical_rebalance_error')
    return {
        'timestamp': _iso(observed_at), 'observation_source': 'binance_read_only_spot_account_tickers_and_futures_account',
        'observation_age_seconds': 0.0, 'spot_real': round(spot_equity, 8), 'futures_real': None if futures_real is None else round(futures_real, 8),
        'observed_equity': None if equity is None else round(equity, 8), 'baseline_spot_unrealized_pnl': None if errors else round(spot_upnl, 8),
        'baseline_futures_unrealized_pnl': futures_upnl, 'baseline_unrealized_pnl': None if baseline is None else round(baseline, 8),
        'open_positions_at_bootstrap': open_positions, 'bot_version': bot_state.get('bot_version') or version_history.current_version(),
        'transfer_active': transfer_active, 'errors': sorted(set(errors)),
        'rebalance': {'persistent_pending': bool(rebalance_status.get('pending')), 'persistent_status': rebalance_status.get('status'), 'derived_status': (bot_state.get('rebalance') or {}).get('status'), 'derived_only': not rebalance_status.get('pending') and (bot_state.get('rebalance') or {}).get('status') == 'PENDING'},
    }


def _event_id(observation):
    payload = '|'.join([PLAN_NAME, observation['timestamp'], str(observation.get('observed_equity')), observation.get('bot_version') or 'unknown'])
    return 'capital-' + hashlib.sha256(payload.encode()).hexdigest()[:24]


def build_plan(ledger_file=capital_ledger.DEFAULT_LEDGER_FILE, observation=None):
    observation = observation or observe_capital()
    records = capital_ledger.read_history(ledger_file)
    initial = [record for record in records if str(record.get('type') or '').lower() == capital_ledger.TYPE_INITIAL_CAPITAL]
    equity = _number(observation.get('observed_equity'))
    spot = _number(observation.get('spot_real'))
    futures = _number(observation.get('futures_real'))
    tolerance = None if equity is None else max(ABS_TOLERANCE_USDT, abs(equity) * PCT_TOLERANCE)
    errors = list(observation.get('errors') or [])
    if initial:
        errors.append('initial_capital_already_exists')
    if None in (equity, spot, futures, _number(observation.get('baseline_unrealized_pnl'))):
        errors.append('incomplete_observation')
    elif abs((spot + futures) - equity) > tolerance:
        errors.append('equity_wallet_sum_mismatch')
    if _number(observation.get('observation_age_seconds')) is None or observation.get('observation_age_seconds', MAX_AGE_SECONDS + 1) > MAX_AGE_SECONDS:
        errors.append('stale_observation')
    event_id = _event_id(observation) if equity is not None else None
    checksum = hashlib.sha256(open(ledger_file, 'rb').read()).hexdigest() if os.path.isfile(ledger_file) else 'MISSING'
    return {'plan': PLAN_NAME, 'mode': 'dry-run', 'ledger': ledger_file, 'ledger_exists': os.path.isfile(ledger_file), 'bootstrap_required': not initial,
            'valid': not errors, 'blocking_reasons': sorted(set(errors)), 'observation': observation, 'proposed_initial_capital': equity,
            'tolerance': tolerance, 'event_id': event_id, 'checksum_before': checksum, 'writes_performed': False,
            'accounting_convention': CONVENTION}


def _atomic_write_initial(plan):
    observation = plan['observation']
    ledger = plan['ledger']
    if capital_ledger.read_history(ledger):
        if any(str(r.get('type') or '').lower() == capital_ledger.TYPE_INITIAL_CAPITAL for r in capital_ledger.read_history(ledger)):
            return {'applied': False, 'idempotent': True, 'event_id': plan['event_id']}
    metadata = {'spot_real': observation['spot_real'], 'futures_real': observation['futures_real'], 'observed_equity': observation['observed_equity'],
                'baseline_unrealized_pnl': observation['baseline_unrealized_pnl'], 'baseline_spot_unrealized_pnl': observation['baseline_spot_unrealized_pnl'],
                'baseline_futures_unrealized_pnl': observation['baseline_futures_unrealized_pnl'], 'open_positions_at_bootstrap': observation['open_positions_at_bootstrap'],
                'observation_source': observation['observation_source'], 'observation_age_seconds': observation['observation_age_seconds'], 'tolerance': plan['tolerance'],
                'accounting_start_timestamp': observation['timestamp'], 'historical_activity_not_reconstructed': True, 'pre_bootstrap_pnl_excluded': True}
    record = capital_ledger._movement_record(capital_ledger.TYPE_INITIAL_CAPITAL, observation['observed_equity'], source='bootstrap_current_observation',
                                             description='Accounting starts at current observation; prior activity is not reconstructed.', reference_id=plan['event_id'], metadata=metadata, timestamp=observation['timestamp'])
    record.update({'event_id': plan['event_id'], 'event_type': 'INITIAL_CAPITAL', 'accounting_convention': CONVENTION, 'amount_usdt': observation['observed_equity'], 'equity_before': observation['observed_equity'], 'equity_after': observation['observed_equity']})
    os.makedirs(os.path.dirname(ledger), exist_ok=True)
    tmp = ledger + '.tmp'
    previous = open(ledger, 'rb').read() if os.path.isfile(ledger) else b''
    with open(tmp, 'wb') as file:
        file.write(previous)
        file.write((json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n').encode())
        file.flush(); os.fsync(file.fileno())
    os.replace(tmp, ledger)
    return {'applied': True, 'idempotent': False, 'event_id': plan['event_id'], 'record': record}


def apply_plan(ledger_file=capital_ledger.DEFAULT_LEDGER_FILE, first_observation=None, observer=observe_capital):
    existing = [r for r in capital_ledger.read_history(ledger_file) if str(r.get('type') or '').lower() == capital_ledger.TYPE_INITIAL_CAPITAL]
    if len(existing) == 1:
        return {'applied': False, 'idempotent': True, 'event_id': existing[0].get('event_id')}
    if len(existing) > 1:
        return {'applied': False, 'idempotent': False, 'reason': 'duplicate_initial_capital'}
    first = build_plan(ledger_file, first_observation or observer())
    if not first['valid']:
        return {'applied': False, 'reason': 'invalid_plan', 'plan': first}
    second = build_plan(ledger_file, observer())
    if not second['valid']:
        return {'applied': False, 'reason': 'reobservation_invalid', 'plan': second}
    tolerance = second['tolerance'] or 0
    fields = ('spot_real', 'futures_real', 'observed_equity', 'baseline_unrealized_pnl')
    changed = {field: abs(float(first['observation'][field]) - float(second['observation'][field])) for field in fields}
    if first['observation'].get('open_positions_at_bootstrap') != second['observation'].get('open_positions_at_bootstrap'):
        changed['open_positions_at_bootstrap'] = 'changed'
    if any((delta == 'changed' or delta > tolerance) for delta in changed.values()):
        second['blocking_reasons'].append('observation_changed_beyond_tolerance')
        return {'applied': False, 'reason': 'observation_changed', 'changes': changed, 'plan': second}
    return _atomic_write_initial(second)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--confirm-plan')
    parser.add_argument('--ledger', default=capital_ledger.DEFAULT_LEDGER_FILE)
    args = parser.parse_args(argv)
    if args.apply:
        if args.confirm_plan != PLAN_NAME:
            print(json.dumps({'applied': False, 'reason': 'confirmation_required'}, indent=2)); return 2
        result = apply_plan(args.ledger)
        print(json.dumps(result, indent=2, sort_keys=True)); return 0 if result.get('applied') or result.get('idempotent') else 2
    print(json.dumps(build_plan(args.ledger), indent=2, sort_keys=True)); return 0


if __name__ == '__main__':
    raise SystemExit(main())
