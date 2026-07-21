#!/usr/bin/env python3
"""Offline replay CLI. It cannot construct or import the production client."""
import argparse
import json
import os
import sys
from decimal import Decimal

if __package__ in (None, ''):
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from testing.replay_scenario_runner import ReplayScenarioRunner
    from testing.replay_fixture_library import get_fixture, load_library
    from testing.replay_scenarios import SCENARIOS, build_replay_scenario
    from testing.replay_tape import ReplayTape
else:
    from .replay_scenario_runner import ReplayScenarioRunner
    from .replay_fixture_library import get_fixture, load_library
    from .replay_scenarios import SCENARIOS, build_replay_scenario
    from .replay_tape import ReplayTape


def _json_default(value):
    if isinstance(value, Decimal): return str(value)
    raise TypeError(type(value).__name__)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run a deterministic offline exchange replay')
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--scenario', choices=sorted(SCENARIOS), default='spot-long-tp')
    source.add_argument('--tape')
    source.add_argument('--incident', choices=sorted(load_library()))
    parser.add_argument('--list', action='store_true'); parser.add_argument('--json', action='store_true')
    parser.add_argument('--output'); parser.add_argument('--strict', action='store_true')
    args = parser.parse_args(argv)
    if args.list:
        print('\n'.join(sorted(SCENARIOS))); return 0
    tape = ReplayTape.load(args.tape) if args.tape else get_fixture(args.incident).tape if args.incident else build_replay_scenario(args.scenario)
    result = ReplayScenarioRunner(tape).run()
    summary = {
        'scenario_id': tape.scenario_id, 'mode': tape.mode, 'fingerprint': tape.fingerprint,
        'events_applied': len(result['applied_events']), 'calls': len(result['exchange']['calls']),
        'complete': tape.complete, 'missing_fields': list(tape.missing_fields),
        'network_fallback': False, 'done': result['cursor']['done'],
    }
    if args.output:
        os.makedirs(args.output, exist_ok=True)
        with open(os.path.join(args.output, 'replay_result.json'), 'w', encoding='utf-8') as handle:
            json.dump(result, handle, default=_json_default, sort_keys=True, indent=2); handle.write('\n')
    if args.json: print(json.dumps(summary, sort_keys=True))
    else:
        print('OFFLINE REPLAY')
        for key, value in summary.items(): print(f'{key}: {value}')
    return 2 if args.strict and not tape.complete else 0


if __name__ == '__main__': raise SystemExit(main())
