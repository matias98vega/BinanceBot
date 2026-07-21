#!/usr/bin/env python3
"""Explicit evidence recorder. It does not control trading or services."""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
import operational_state

ACTIONS = {
    'maintenance-start': ('MAINTENANCE', 'MAINTENANCE_STARTED'),
    'maintenance-end': ('RUNNING', 'MAINTENANCE_FINISHED'),
    'manual-pause-start': ('PAUSED_MANUAL', 'MANUAL_PAUSE_STARTED'),
    'manual-pause-end': ('RUNNING', 'MANUAL_PAUSE_FINISHED'),
    'note': ('RUNNING', 'OPERATIONAL_NOTE'),
}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Record explicit operational evidence only')
    parser.add_argument('action', choices=sorted(ACTIONS)); parser.add_argument('--reason', required=True)
    parser.add_argument('--actor', required=True); parser.add_argument('--idempotency-key'); parser.add_argument('--path', default=operational_state.DEFAULT_FILE)
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    event_id = args.idempotency_key or hashlib.sha256(f'{args.action}|{args.actor}|{args.reason}|{now}'.encode()).hexdigest()[:24]
    if any(row.get('event_id') == event_id for row in operational_state.iter_events(args.path)):
        print(json.dumps({'recorded': False, 'idempotent': True, 'event_id': event_id})); return 0
    state, reason_code = ACTIONS[args.action]
    record = operational_state.append_event({
        'event_id': event_id, 'event_type': args.action.replace('-', '_'), 'event_category': 'OPERATIONAL',
        'severity': 'INFO', 'state': state, 'reason_code': reason_code, 'started_at': now,
        'observed_at': now, 'source': 'manual_cli', 'actor': args.actor,
        'summary': args.reason, 'evidence': {'reason': args.reason}, 'confidence': 'EXPLICIT',
        'blocking_new_entries': state in {'MAINTENANCE', 'PAUSED_MANUAL'}, 'blocking_position_management': False,
    }, args.path)
    print(json.dumps({'recorded': True, 'event_id': event_id, 'event': record}, ensure_ascii=False))
    return 0


if __name__ == '__main__': raise SystemExit(main())
