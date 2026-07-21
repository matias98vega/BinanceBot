#!/usr/bin/env python3
"""Append-only operational state transitions and spaced heartbeats."""
import json
import os
import sys
from datetime import datetime, timezone

TRADING_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TRADING_DIR)
DEFAULT_FILE = os.path.join(PROJECT_DIR, 'data', 'history', 'operational_state.jsonl')
SCHEMA_VERSION = 1
STATES = {
    'RUNNING', 'IDLE_NO_SIGNAL', 'PAUSED_RISK', 'PAUSED_MANUAL',
    'BLOCKED_CAPACITY', 'BLOCKED_RECONCILIATION', 'BLOCKED_EXCHANGE_UNKNOWN',
    'MAINTENANCE', 'DEGRADED', 'STOPPED_UNKNOWN', 'ERROR',
}
HEARTBEAT_INTERVAL_SECONDS = int(os.getenv('OPERATIONAL_HEARTBEAT_INTERVAL_SECONDS', '900'))


def _iso(value=None):
    if value is None:
        value = datetime.now(timezone.utc)
    elif isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, timezone.utc)
    elif isinstance(value, str):
        return value
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _epoch(value):
    try: return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except Exception: return None


def iter_events(path=DEFAULT_FILE):
    try:
        with open(path, encoding='utf-8') as handle:
            for line in handle:
                try: row = json.loads(line)
                except Exception: continue
                if isinstance(row, dict): yield row
    except FileNotFoundError:
        return


def latest_event(path=DEFAULT_FILE, event_type=None):
    latest = None
    for row in iter_events(path):
        if event_type and row.get('event_type') != event_type: continue
        latest = row
    return latest


def _test_write_blocked(path):
    if os.path.abspath(path) != os.path.abspath(DEFAULT_FILE): return False
    argv = ' '.join(sys.argv).lower()
    return 'unittest' in argv or 'discover' in argv or str(os.getenv('BINANCEBOT_TEST_MODE', '')).lower() in {'1', 'true', 'yes'}


def append_event(record, path=DEFAULT_FILE):
    record = dict(record)
    record.setdefault('schema_version', SCHEMA_VERSION)
    record.setdefault('recorded_at', _iso())
    if _test_write_blocked(path): return record
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = (json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o640)
    try: os.write(fd, payload)
    finally: os.close(fd)
    return record


def transition(state, reason_code, *, cycle_id=None, source='bot', evidence=None,
               expected_until=None, blocking_new_entries=False,
               blocking_position_management=False, confidence='HIGH', observed_at=None,
               path=DEFAULT_FILE, force=False):
    state = str(state).upper()
    if state not in STATES: raise ValueError(f'unsupported operational state: {state}')
    observed_at = _iso(observed_at)
    previous = latest_event(path, "operational_state_transition")
    if not force and previous and previous.get('event_type') == 'operational_state_transition' and previous.get('state') == state and previous.get('reason_code') == reason_code:
        return None
    return append_event({
        'event_type': 'operational_state_transition', 'event_category': 'OPERATIONAL',
        'severity': 'INFO' if state not in {'ERROR', 'DEGRADED', 'BLOCKED_EXCHANGE_UNKNOWN'} else 'WARNING',
        'state': state, 'reason_code': reason_code, 'started_at': observed_at,
        'observed_at': observed_at, 'expected_until': expected_until, 'cycle_id': cycle_id,
        'source': source, 'evidence': evidence or {},
        'blocking_new_entries': bool(blocking_new_entries),
        'blocking_position_management': bool(blocking_position_management),
        'confidence': confidence,
    }, path)


def heartbeat(*, state='RUNNING', cycle_id=None, last_successful_cycle_at=None,
              last_exchange_observation_at=None, positions_managed=0, positions_observed=None,
              new_entries_allowed=None, pause_until=None, guardian_status=None,
              pre_entry_gate_mode=None, source='cycle_runner', observed_at=None,
              interval_seconds=None, path=DEFAULT_FILE):
    observed_at = _iso(observed_at)
    previous = latest_event(path, 'operational_heartbeat')
    interval = HEARTBEAT_INTERVAL_SECONDS if interval_seconds is None else int(interval_seconds)
    if previous:
        before, now = _epoch(previous.get('observed_at')), _epoch(observed_at)
        if before is not None and now is not None and now - before < interval: return None
    return append_event({
        'event_type': 'operational_heartbeat', 'event_category': 'OPERATIONAL', 'severity': 'INFO',
        'state': state, 'reason_code': 'PERIODIC_EVIDENCE', 'observed_at': observed_at,
        'cycle_id': cycle_id, 'last_successful_cycle_at': last_successful_cycle_at,
        'last_exchange_observation_at': last_exchange_observation_at,
        'positions_managed': positions_managed, 'positions_observed': positions_observed,
        'new_entries_allowed': new_entries_allowed, 'pause_until': pause_until,
        'guardian_status': guardian_status, 'pre_entry_gate_mode': pre_entry_gate_mode,
        'source': source, 'confidence': 'HIGH',
    }, path)


def cycle_completed(*, cycle_id, started_at, completed_at=None, duration_ms=None,
                    operational_state='IDLE_NO_SIGNAL', reason_code='NO_SIGNAL', summary=None,
                    path=DEFAULT_FILE, **metrics):
    return append_event({
        'event_type': 'cycle_completed', 'event_category': 'OPERATIONAL', 'severity': 'INFO',
        'cycle_id': cycle_id, 'started_at': started_at, 'completed_at': _iso(completed_at),
        'observed_at': _iso(completed_at), 'duration_ms': duration_ms,
        'state': operational_state, 'reason_code': reason_code,
        'summary': summary or 'Cycle completed', 'source': 'cycle_runner',
        **metrics,
    }, path)
