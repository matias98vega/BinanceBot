#!/usr/bin/env python3
"""Healthcheck local de observabilidad y estado operativo."""
import json
import os
import time
from collections import defaultdict
from config_loader import load_config


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_runtime_config = load_config(require_api=False)
STATE_FILE = _runtime_config.state_file
TRADE_ANALYTICS = _runtime_config.analytics_file
DECISION_SNAPSHOTS = _runtime_config.decision_snapshots_file
TRADES_LOG = _runtime_config.trades_log
LOCK_FILE = _runtime_config.lock_file


def _load_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f), None
    except Exception as e:
        return None, str(e)


def _read_jsonl(path):
    records = []
    if not os.path.exists(path):
        return records
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _age(path):
    if not os.path.exists(path):
        return None
    return time.time() - os.path.getmtime(path)


def _fmt_age(path):
    age = _age(path)
    if age is None:
        return 'MISSING'
    minutes = age / 60
    hours = minutes / 60
    if hours >= 1:
        return f'{hours:.2f}h'
    return f'{minutes:.2f}m'


def _pid_exists(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _lock_info(path):
    if not os.path.exists(path):
        return {'status': 'ABSENT', 'pid': None, 'created_at': None}
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return {'status': 'STALE', 'pid': None, 'created_at': None}

    pid = data.get('pid')
    created_at = data.get('created_at')
    if _pid_exists(pid):
        return {'status': 'PRESENT', 'pid': pid, 'created_at': created_at}
    return {'status': 'STALE', 'pid': pid, 'created_at': created_at}


def _analytics_open_trades(records):
    merged = {}
    for record in records:
        trade_id = record.get('trade_id')
        if not trade_id:
            continue
        merged.setdefault(trade_id, {}).update({k: v for k, v in record.items() if v is not None})
    return [r for r in merged.values() if r.get('status') == 'OPEN']


def main():
    state_exists = os.path.exists(STATE_FILE)
    state, state_error = _load_json(STATE_FILE) if state_exists else (None, 'missing')
    state_valid = state_exists and state_error is None
    positions = state.get('positions', []) if isinstance(state, dict) else []

    analytics_records = _read_jsonl(TRADE_ANALYTICS)
    analytics_open = _analytics_open_trades(analytics_records)
    recovered_open = [t for t in analytics_open if t.get('recovered_existing_position') is True]
    state_ids = {p.get('id') for p in positions if p.get('id')}
    analytics_ids = {t.get('trade_id') for t in analytics_open if t.get('trade_id')}
    missing_in_analytics = sorted(state_ids - analytics_ids)
    missing_in_state = sorted(analytics_ids - state_ids)
    lock = _lock_info(LOCK_FILE)

    warnings = []
    errors = []
    if not state_valid:
        errors.append('state.json missing or invalid')
    if not os.path.exists(TRADE_ANALYTICS):
        errors.append('trade_analytics.jsonl missing')
    if not os.path.exists(DECISION_SNAPSHOTS):
        errors.append('decision_snapshots.jsonl missing')
    if missing_in_analytics:
        warnings.append('open state positions missing in analytics')
    if missing_in_state:
        warnings.append('analytics OPEN trades missing in state')
    if lock['status'] == 'PRESENT':
        warnings.append('bot lock file exists with active process')
    elif lock['status'] == 'STALE':
        warnings.append('bot lock file is stale')

    final_status = 'OK'
    if errors:
        final_status = 'ERROR'
    elif warnings:
        final_status = 'WARNING'

    print('OBSERVABILITY HEALTHCHECK')
    print('')
    print('Files:')
    print(f'- state.json: {"OK" if state_valid else "ERROR"}')
    if state_error and state_error != 'missing':
        print(f'  - error: {state_error}')
    print(f'- Lock file: {LOCK_FILE}')
    print(f'- bot.lock: {lock["status"]}')
    if lock.get('pid') is not None:
        print(f'  - pid: {lock["pid"]}')
    if lock.get('created_at'):
        print(f'  - created_at: {lock["created_at"]}')
    print(f'- trade_analytics.jsonl age: {_fmt_age(TRADE_ANALYTICS)}')
    print(f'- decision_snapshots.jsonl age: {_fmt_age(DECISION_SNAPSHOTS)}')
    print(f'- trades_log.txt age: {_fmt_age(TRADES_LOG)}')
    print('')
    print('State:')
    print(f'- open positions in state.json: {len(positions)}')
    print('')
    print('Analytics:')
    print(f'- OPEN trades in trade_analytics.jsonl: {len(analytics_open)}')
    print(f'- recovered OPEN trades: {len(recovered_open)}')
    print('')
    print('Alignment:')
    print(f'- state positions missing in analytics: {len(missing_in_analytics)}')
    for trade_id in missing_in_analytics[:10]:
        print(f'  - {trade_id}')
    print(f'- analytics OPEN trades missing in state: {len(missing_in_state)}')
    for trade_id in missing_in_state[:10]:
        print(f'  - {trade_id}')
    print('')
    print('Warnings:')
    if warnings:
        for warning in warnings:
            print(f'- {warning}')
    else:
        print('- none')
    print('')
    print('Errors:')
    if errors:
        for error in errors:
            print(f'- {error}')
    else:
        print('- none')
    print('')
    print('Final status:')
    print(final_status)


if __name__ == '__main__':
    main()
