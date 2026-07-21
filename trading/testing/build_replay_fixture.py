#!/usr/bin/env python3
"""Validate or export sanitized incident fixtures; never reads production data."""
import argparse
import json
import os
import sys

if __package__ in (None, ''):
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from testing.replay_fixture_library import get_fixture, load_library
else:
    from .replay_fixture_library import get_fixture, load_library


def main(argv=None):
    parser = argparse.ArgumentParser(description='Validate permanent sanitized replay fixtures')
    parser.add_argument('--list', action='store_true'); parser.add_argument('--validate-all', action='store_true')
    parser.add_argument('--fixture'); parser.add_argument('--json', action='store_true'); parser.add_argument('--output')
    args = parser.parse_args(argv)
    library = load_library()
    if args.list:
        print('\n'.join(sorted(library))); return 0
    if args.validate_all:
        summary = {'valid': True, 'count': len(library), 'fixtures': {key: value.fingerprint for key, value in sorted(library.items())}}
    else:
        if not args.fixture: parser.error('--fixture is required unless --list or --validate-all is used')
        fixture = get_fixture(args.fixture)
        summary = {'valid': True, 'scenario_id': fixture.scenario_id, 'fixture_fingerprint': fixture.fingerprint,
                   'tape_fingerprint': fixture.tape.fingerprint, 'fidelity': fixture.payload['fidelity'],
                   'confidence': fixture.payload['confidence'], 'known_missing_fields': fixture.payload['known_missing_fields']}
        if args.output:
            os.makedirs(args.output, exist_ok=True)
            with open(os.path.join(args.output, fixture.scenario_id + '.tape.json'), 'w', encoding='utf-8') as handle:
                json.dump(fixture.tape.as_dict(), handle, ensure_ascii=False, indent=2, sort_keys=True); handle.write('\n')
    if args.json: print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    else:
        print('SANITIZED REPLAY FIXTURES')
        for key, value in summary.items(): print(f'{key}: {value}')
    return 0


if __name__ == '__main__': raise SystemExit(main())
