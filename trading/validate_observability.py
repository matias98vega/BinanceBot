#!/usr/bin/env python3
"""Validacion local de integridad de observabilidad."""
import json
import os
from collections import Counter, defaultdict
from config_loader import load_config


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_runtime_config = load_config(require_api=False)
TRADE_ANALYTICS = _runtime_config.analytics_file
DECISION_SNAPSHOTS = _runtime_config.decision_snapshots_file
TRADES_LOG = _runtime_config.trades_log
STATE_FILE = _runtime_config.state_file

OPEN_REQUIRED = ['trade_id', 'symbol', 'side', 'entry_time', 'entry_price', 'status']
CLOSED_REQUIRED = [
    'trade_id', 'symbol', 'side', 'entry_time', 'exit_time', 'entry_price',
    'exit_price', 'exit_reason', 'pnl_usdt', 'status',
]
NULL_FIELDS = ['ema20', 'ema50', 'volume_ratio', 'macd_hist', 'atr_pct', 'btc_correlation']


def _read_jsonl(path):
    records = []
    corrupt = 0
    if not os.path.exists(path):
        return records, corrupt
    with open(path, encoding='utf-8') as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                corrupt += 1
    return records, corrupt


def _missing_fields(record, required):
    missing = []
    for field in required:
        if record.get(field) is None:
            missing.append(field)
    return missing


def _size(path):
    if not os.path.exists(path):
        return None
    return os.path.getsize(path)


def _fmt_size(path):
    size = _size(path)
    if size is None:
        return 'MISSING'
    return f'{size} bytes'


def _currently_open_trades(records):
    latest_status = {}
    for record in records:
        trade_id = record.get('trade_id')
        status = record.get('status')
        if trade_id and status in {'OPEN', 'CLOSED'}:
            latest_status[trade_id] = status
    return [trade_id for trade_id, status in latest_status.items() if status == 'OPEN']


def main():
    analytics_exists = os.path.exists(TRADE_ANALYTICS)
    decisions_exists = os.path.exists(DECISION_SNAPSHOTS)
    trade_records, corrupt_trades = _read_jsonl(TRADE_ANALYTICS)
    decision_records, corrupt_decisions = _read_jsonl(DECISION_SNAPSHOTS)

    opens = [r for r in trade_records if r.get('status') == 'OPEN']
    closes = [r for r in trade_records if r.get('status') == 'CLOSED']
    currently_open = _currently_open_trades(trade_records)
    open_counts = Counter(r.get('trade_id') for r in opens if r.get('trade_id'))
    close_counts = Counter(r.get('trade_id') for r in closes if r.get('trade_id'))
    opened_ids = set(open_counts)

    duplicated_opens = sum(1 for count in open_counts.values() if count > 1)
    duplicated_closes = sum(1 for count in close_counts.values() if count > 1)
    closes_without_open = sum(1 for r in closes if r.get('trade_id') not in opened_ids)

    missing_required = 0
    missing_details = defaultdict(int)
    for record in opens:
        missing = _missing_fields(record, OPEN_REQUIRED)
        if missing:
            missing_required += 1
            for field in missing:
                missing_details[f'OPEN.{field}'] += 1
    for record in closes:
        missing = _missing_fields(record, CLOSED_REQUIRED)
        if missing or record.get('status') != 'CLOSED':
            missing_required += 1
            for field in missing:
                missing_details[f'CLOSED.{field}'] += 1

    null_counts = Counter()
    for record in opens:
        for field in NULL_FIELDS:
            if record.get(field) is None:
                null_counts[field] += 1

    snapshots_without_candidates = 0
    for snapshot in decision_records:
        candidates = snapshot.get('candidates')
        if not candidates:
            snapshots_without_candidates += 1

    final_status = 'OK'
    if (
        not analytics_exists or not decisions_exists or corrupt_trades or corrupt_decisions
        or duplicated_opens or duplicated_closes or closes_without_open or missing_required
    ):
        final_status = 'ERROR'
    elif snapshots_without_candidates or any(null_counts.values()):
        final_status = 'WARNING'

    print('OBSERVABILITY VALIDATION')
    print('')
    print('Files:')
    print(f'- trade_analytics.jsonl: {"OK" if analytics_exists else "MISSING"}')
    print(f'- decision_snapshots.jsonl: {"OK" if decisions_exists else "MISSING"}')
    print('')
    print('JSONL:')
    print(f'- corrupt lines: {corrupt_trades + corrupt_decisions}')
    print('')
    print('Trades:')
    print(f'- open events: {len(opens)}')
    print(f'- closed events: {len(closes)}')
    print(f'- currently open trades: {len(currently_open)}')
    print(f'- duplicated opens: {duplicated_opens}')
    print(f'- duplicated closes: {duplicated_closes}')
    print(f'- closes without open: {closes_without_open}')
    print(f'- missing required fields: {missing_required}')
    if missing_details:
        for field, count in sorted(missing_details.items()):
            print(f'  - {field}: {count}')
    print('')
    print('Snapshots:')
    print(f'- total snapshots: {len(decision_records)}')
    print(f'- snapshots without candidates: {snapshots_without_candidates}')
    print('')
    print('Null fields:')
    for field in NULL_FIELDS:
        print(f'- {field}: {null_counts[field]}')
    print('')
    print('File sizes:')
    print(f'- trade_analytics.jsonl: {_fmt_size(TRADE_ANALYTICS)}')
    print(f'- decision_snapshots.jsonl: {_fmt_size(DECISION_SNAPSHOTS)}')
    print(f'- trades_log.txt: {_fmt_size(TRADES_LOG)}')
    print(f'- state.json: {_fmt_size(STATE_FILE)}')
    print('')
    print('Final status:')
    print(final_status)


if __name__ == '__main__':
    main()
