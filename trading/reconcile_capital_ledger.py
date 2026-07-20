#!/usr/bin/env python3
"""Dry-run planner for capital ledger bootstrap and unexplained flows."""
import argparse
import json
import os

import capital_ledger


def build_plan(ledger_file=capital_ledger.DEFAULT_LEDGER_FILE):
    exists = os.path.isfile(ledger_file)
    records = capital_ledger.read_history(ledger_file)
    return {'plan': 'capital-ledger-bootstrap', 'mode': 'dry-run', 'ledger': ledger_file, 'ledger_exists': exists, 'events': len(records), 'bootstrap_required': not exists or not any(r.get('type') == capital_ledger.TYPE_INITIAL_CAPITAL for r in records), 'recommended_event': {'event_type': 'INITIAL_CAPITAL', 'source': 'bootstrap_current_observation', 'reason': 'Historical movements were not reconstructed; observe current Spot, Futures and equity before explicit apply.'}, 'writes_performed': False, 'limitations': ['No BinanceClient history queried', 'No historical deposits inferred', 'Equity changes without reliable evidence remain UNKNOWN_CAPITAL_FLOW']}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--confirm-plan')
    parser.add_argument('--ledger', default=capital_ledger.DEFAULT_LEDGER_FILE)
    args = parser.parse_args(argv)
    if args.apply:
        print('Apply disabled in this implementation task; review the dry-run plan and implement an explicit observed-capital input before bootstrap.')
        return 2
    print(json.dumps(build_plan(args.ledger), indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
