#!/usr/bin/env python3
"""Read-only CLI for the state-vs-exchange pre-entry gate."""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import binance_client
import bot_state as bot_state_module
import pre_entry_safety_gate

TRADING_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE = os.path.join(TRADING_DIR, 'state.json')
DEFAULT_BOT_STATE = os.path.join(TRADING_DIR, 'bot_state.json')


def _read(path):
    try:
        with open(path, encoding='utf-8') as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _capacity(bot_state, side, local_state):
    bucket = ((bot_state.get('positions') or {}).get(side.lower()) or {})
    current = bucket.get('current')
    if current is None:
        current = sum(1 for pos in local_state.get('positions', []) if str(pos.get('direction')).upper() == side)
    return {'current': current, 'operational_max': bucket.get('operational_max', bucket.get('max')),
            'target_max': bucket.get('target_max'), 'new_entries_allowed': bucket.get('new_entries_allowed')}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only pre-entry state/exchange safety check')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--explain', action='store_true')
    parser.add_argument('--side', choices=('LONG', 'SHORT'), default='LONG')
    parser.add_argument('--symbol', default='BTCUSDT')
    parser.add_argument('--state', default=DEFAULT_STATE)
    parser.add_argument('--bot-state', default=DEFAULT_BOT_STATE)
    parser.add_argument('--strict', action='store_true')
    parser.add_argument('--offline-fixture', help='JSON exchange observation; avoids all network access')
    args = parser.parse_args(argv)
    local_state, observable = _read(args.state), _read(args.bot_state)
    context = {'capacity': _capacity(observable, args.side, local_state)}
    client = None
    if args.offline_fixture:
        context['exchange_observation'] = _read(args.offline_fixture)
    else:
        client = binance_client.get_default_client()
    reconciliation = ((observable.get('positions') or {}).get('short') or {}).get('reconciliation') or {}
    result = pre_entry_safety_gate.evaluate_pre_entry_safety(
        client=client, local_state=local_state, bot_state=observable, side=args.side,
        symbol=args.symbol, reconciliation_status=reconciliation, context=context,
        mode=pre_entry_safety_gate.configured_mode(),
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print('PRE-ENTRY SAFETY')
        print(f'Status: {result["status"]}')
        print(f'Safe to enter: {str(result["safe_to_enter"]).lower()}')
        print(f'Mode: {result["mode"]}')
        print(f'Side/Symbol: {result["side"]} {result["symbol"]}')
        print(f'Freshness: {result["freshness"]["exchange"]}')
        print(f'Duration: {result["duration_ms"]:.3f} ms')
        print('Reasons: ' + (', '.join(result['reasons']) if result['reasons'] else 'none'))
        if args.explain:
            for name, check in result['checks'].items():
                label = 'PASS' if check['passed'] else 'BLOCK'
                print(f'- {name}: {label} [{check["status_code"]}] {check["reason"]}')
    return 2 if args.strict and not result['safe_to_enter'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
