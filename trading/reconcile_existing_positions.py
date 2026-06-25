#!/usr/bin/env python3
"""Reconcile local open positions created before analytics existed."""
import json
import os
from datetime import datetime, timezone
from config_loader import load_config


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_runtime_config = load_config(require_api=False)
STATE_FILE = _runtime_config.state_file
TRADE_ANALYTICS = _runtime_config.analytics_file

RECOVERY_REASON = 'position_existed_before_analytics'


def _iso_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _iso_from_epoch(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    except Exception:
        return None


def _float_or_none(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_state(path):
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return {}
    return data


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


def _open_trade_ids(records):
    return {
        record.get('trade_id')
        for record in records
        if record.get('trade_id') and record.get('status') == 'OPEN'
    }


def _side(position):
    direction = str(position.get('direction') or '').upper()
    if direction == 'LONG':
        return 'LONG'
    if direction == 'SHORT':
        return 'SHORT'
    return None


def _build_recovered_open(position, recovered_at):
    entry_time = _iso_from_epoch(position.get('entry_time'))
    entry_price = _float_or_none(position.get('entry_price'))

    return {
        'trade_id': position.get('id'),
        'symbol': position.get('symbol'),
        'side': _side(position),
        'entry_time': entry_time,
        'entry_price': entry_price,
        'market_regime': None,
        'score': None,
        'rsi': None,
        'atr': _float_or_none(position.get('atr')),
        'atr_pct': None,
        'ema20': None,
        'ema50': None,
        'macd_hist': None,
        'volume_ratio': None,
        'btc_correlation': None,
        'reject_reason': None,
        'reject_reasons': None,
        'capital_at_entry': None,
        'status': 'OPEN',
        'recovered_existing_position': True,
        'recovery_reason': RECOVERY_REASON,
        'analytics_recovered_at': recovered_at,
        'entry_time_recovered': entry_time is not None,
        'entry_price_recovered': entry_price is not None,
        'quantity': _float_or_none(position.get('quantity')),
        'sl': _float_or_none(position.get('sl')),
        'tp': _float_or_none(position.get('tp')),
    }


def _append_records(path, records):
    if not records:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'a', encoding='utf-8') as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n')


def reconcile():
    state = _load_state(STATE_FILE)
    positions = state.get('positions', [])
    if not isinstance(positions, list):
        positions = []

    analytics_records = _read_jsonl(TRADE_ANALYTICS)
    existing_open_ids = _open_trade_ids(analytics_records)
    recovered_at = _iso_now()

    records_to_append = []
    skipped_existing = []
    skipped_missing_id = []

    for position in positions:
        if not isinstance(position, dict):
            continue
        trade_id = position.get('id')
        if not trade_id:
            skipped_missing_id.append(position.get('symbol') or 'UNKNOWN')
            continue
        if trade_id in existing_open_ids:
            skipped_existing.append(trade_id)
            continue
        records_to_append.append(_build_recovered_open(position, recovered_at))

    _append_records(TRADE_ANALYTICS, records_to_append)

    return {
        'state_positions': len(positions),
        'analytics_open_before': len(existing_open_ids),
        'reconciled': records_to_append,
        'skipped_existing': skipped_existing,
        'skipped_missing_id': skipped_missing_id,
    }


def main():
    try:
        result = reconcile()
    except FileNotFoundError:
        print('RECONCILE EXISTING POSITIONS')
        print('')
        print('ERROR: state.json not found')
        return 1
    except json.JSONDecodeError as exc:
        print('RECONCILE EXISTING POSITIONS')
        print('')
        print(f'ERROR: state.json invalid JSON: {exc}')
        return 1

    print('RECONCILE EXISTING POSITIONS')
    print('')
    print(f'- positions in state.json: {result["state_positions"]}')
    print(f'- analytics OPEN before reconcile: {result["analytics_open_before"]}')
    print(f'- reconciled positions: {len(result["reconciled"])}')
    for record in result['reconciled']:
        print(f'  - {record.get("trade_id")} {record.get("symbol")} {record.get("side")}')
    print(f'- skipped existing OPEN records: {len(result["skipped_existing"])}')
    print(f'- skipped missing id: {len(result["skipped_missing_id"])}')
    print('')
    print('Final status:')
    print('OK')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
