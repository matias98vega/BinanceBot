#!/usr/bin/env python3
"""Passive append-only ledger for capital movements."""
import hashlib
import json
import logging
import math
import os
from datetime import datetime, timezone

import history
import version_history


DEFAULT_LEDGER_FILE = os.path.join(history.DEFAULT_HISTORY_DIR, 'capital_ledger.jsonl')

TYPE_EXTERNAL_DEPOSIT = 'external_deposit'
TYPE_EXTERNAL_WITHDRAWAL = 'external_withdrawal'
TYPE_REBALANCE = 'rebalance'
TYPE_REALIZED_PNL = 'realized_pnl'
TYPE_COMMISSION = 'commission'
TYPE_FUNDING_FEE = 'funding_fee'
TYPE_INITIAL_CAPITAL = 'initial_capital'
TYPE_MANUAL_ADJUSTMENT = 'manual_adjustment'
TYPE_RECONCILIATION = 'reconciliation'
TYPE_UNKNOWN_CAPITAL_FLOW = 'unknown_capital_flow'
ACCOUNTING_CONVENTION = 'realized_pnl_net_of_trading_fees_v1'

SUPPORTED_TYPES = {
    TYPE_EXTERNAL_DEPOSIT,
    TYPE_EXTERNAL_WITHDRAWAL,
    TYPE_REBALANCE,
    TYPE_REALIZED_PNL,
    TYPE_COMMISSION,
    TYPE_FUNDING_FEE,
    TYPE_INITIAL_CAPITAL,
    TYPE_MANUAL_ADJUSTMENT,
    TYPE_RECONCILIATION,
    TYPE_UNKNOWN_CAPITAL_FLOW,
}

SENSITIVE_MARKERS = ('key', 'secret', 'token', 'signature', 'header', 'cookie', 'authorization')


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _iso(value=None):
    if value is None:
        return _now_iso()
    if isinstance(value, str):
        return value
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    except Exception:
        return None


def _float_or_none(value):
    try:
        if value is None or value == '':
            return None
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return None
        return result
    except (TypeError, ValueError):
        return None


def _sanitize(value, depth=0):
    if depth > 5:
        return None
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            key_s = str(key)
            if any(marker in key_s.lower() for marker in SENSITIVE_MARKERS):
                continue
            clean[key_s] = _sanitize(item, depth + 1)
        return clean
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(item, depth + 1) for item in list(value)[:100]]
    if isinstance(value, float):
        return _float_or_none(value)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def _normalise_type(movement_type):
    value = str(movement_type or '').strip().lower()
    if not value:
        raise ValueError('movement type is required')
    return value


def _movement_record(movement_type, amount, asset='USDT', source=None, description=None,
                     reference_id=None, metadata=None, timestamp=None):
    amount_f = _float_or_none(amount)
    if amount_f is None:
        raise ValueError('amount must be numeric')
    movement_type = _normalise_type(movement_type)
    if movement_type in {TYPE_INITIAL_CAPITAL, TYPE_EXTERNAL_DEPOSIT, TYPE_EXTERNAL_WITHDRAWAL, TYPE_COMMISSION} and amount_f < 0:
        raise ValueError('amount must be non-negative for this event type')
    return {
        'schema_version': 2,
        'accounting_convention': ACCOUNTING_CONVENTION,
        'recorded_at': _now_iso(),
        'timestamp': _iso(timestamp),
        'type': movement_type,
        'event_type': movement_type.upper(),
        'amount': amount_f,
        'amount_usdt': amount_f if str(asset or 'USDT').upper() == 'USDT' else None,
        'wallet': (metadata or {}).get('wallet'),
        'direction': (metadata or {}).get('direction'),
        'reason': description,
        'related_trade_id': (metadata or {}).get('related_trade_id'),
        'related_rebalance_id': (metadata or {}).get('related_rebalance_id'),
        'binance_reference': (metadata or {}).get('binance_reference'),
        'balance_before': (metadata or {}).get('balance_before'),
        'balance_after': (metadata or {}).get('balance_after'),
        'equity_before': (metadata or {}).get('equity_before'),
        'equity_after': (metadata or {}).get('equity_after'),
        'asset': str(asset or 'USDT').upper(),
        'source': source,
        'description': description,
        'reference_id': reference_id,
        'metadata': _sanitize(metadata or {}),
        'bot_version': version_history.current_version(),
    }

def _event_id(record):
    identity = record.get("reference_id") or record.get("metadata", {}).get("binance_reference")
    payload = {key: record.get(key) for key in ("type", "timestamp", "asset", "amount", "source")}
    payload["identity"] = identity
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"capital-{digest[:24]}"


def _event_exists(path, event_id):
    return any(record.get("event_id") == event_id for record in read_history(path))



def _append(path, record):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')
        return True
    except Exception as exc:
        logging.warning('capital ledger append failed path=%s error=%s', path, exc)
        return False


def record_movement(movement_type, amount, asset='USDT', source=None, description=None,
                    reference_id=None, metadata=None, timestamp=None, ledger_file=DEFAULT_LEDGER_FILE):
    record = _movement_record(
        movement_type,
        amount,
        asset=asset,
        source=source,
        description=description,
        reference_id=reference_id,
        metadata=metadata,
        timestamp=timestamp,
    )
    record["event_id"] = _event_id(record)
    if _event_exists(ledger_file, record["event_id"]):
        record["duplicate"] = True
        return record
    _append(ledger_file, record)
    return record


def read_history(ledger_file=DEFAULT_LEDGER_FILE, movement_type=None, asset=None, limit=None):
    records = []
    type_filter = str(movement_type or '').strip().lower() or None
    asset_filter = str(asset or '').strip().upper() or None
    try:
        with open(ledger_file, encoding='utf-8') as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    logging.warning('capital ledger JSONL invalid path=%s line=%s error=%s', ledger_file, lineno, exc)
                    continue
                if not isinstance(record, dict):
                    continue
                if type_filter and str(record.get('type') or '').lower() != type_filter:
                    continue
                if asset_filter and str(record.get('asset') or '').upper() != asset_filter:
                    continue
                records.append(record)
    except FileNotFoundError:
        return []
    except Exception as exc:
        logging.warning('capital ledger read failed path=%s error=%s', ledger_file, exc)
        return []
    if limit is not None:
        try:
            return records[-int(limit):]
        except (TypeError, ValueError):
            return records
    return records


def get_totals_by_type(ledger_file=DEFAULT_LEDGER_FILE, asset=None):
    totals = {}
    for record in read_history(ledger_file=ledger_file, asset=asset):
        movement_type = str(record.get('type') or 'unknown')
        amount = _float_or_none(record.get('amount')) or 0.0
        totals[movement_type] = round(totals.get(movement_type, 0.0) + amount, 8)
    return totals


def get_net_deposits(ledger_file=DEFAULT_LEDGER_FILE, asset=None):
    return round(get_totals_by_type(ledger_file=ledger_file, asset=asset).get(TYPE_EXTERNAL_DEPOSIT, 0.0), 8)


def get_net_withdrawals(ledger_file=DEFAULT_LEDGER_FILE, asset=None):
    return round(get_totals_by_type(ledger_file=ledger_file, asset=asset).get(TYPE_EXTERNAL_WITHDRAWAL, 0.0), 8)


def get_external_capital_summary(ledger_file=DEFAULT_LEDGER_FILE, asset=None):
    deposits = get_net_deposits(ledger_file=ledger_file, asset=asset)
    withdrawals = get_net_withdrawals(ledger_file=ledger_file, asset=asset)
    return {
        'external_deposits': deposits,
        'external_withdrawals': withdrawals,
        'external_net': round(deposits - withdrawals, 8),
    }


def estimate_adjusted_pnl(current_capital, ledger_file=DEFAULT_LEDGER_FILE, asset=None):
    capital = _float_or_none(current_capital)
    if capital is None:
        return None
    summary = get_external_capital_summary(ledger_file=ledger_file, asset=asset)
    return round(capital - summary['external_deposits'] + summary['external_withdrawals'], 8)


def register_external_deposit(amount, asset='USDT', source='manual', description=None,
                              reference_id=None, metadata=None, timestamp=None, ledger_file=DEFAULT_LEDGER_FILE):
    return record_movement(TYPE_EXTERNAL_DEPOSIT, amount, asset, source, description, reference_id, metadata, timestamp, ledger_file)


def register_external_withdrawal(amount, asset='USDT', source='manual', description=None,
                                 reference_id=None, metadata=None, timestamp=None, ledger_file=DEFAULT_LEDGER_FILE):
    return record_movement(TYPE_EXTERNAL_WITHDRAWAL, amount, asset, source, description, reference_id, metadata, timestamp, ledger_file)


def register_rebalance(amount, asset='USDT', source='bot', description=None,
                       reference_id=None, metadata=None, timestamp=None, ledger_file=DEFAULT_LEDGER_FILE):
    return record_movement(TYPE_REBALANCE, amount, asset, source, description, reference_id, metadata, timestamp, ledger_file)


def register_commission(amount, asset='USDT', source='binance', description=None,
                        reference_id=None, metadata=None, timestamp=None, ledger_file=DEFAULT_LEDGER_FILE):
    return record_movement(TYPE_COMMISSION, amount, asset, source, description, reference_id, metadata, timestamp, ledger_file)


def register_funding_fee(amount, asset='USDT', source='binance', description=None,
                         reference_id=None, metadata=None, timestamp=None, ledger_file=DEFAULT_LEDGER_FILE):
    return record_movement(TYPE_FUNDING_FEE, amount, asset, source, description, reference_id, metadata, timestamp, ledger_file)


def register_realized_pnl(amount, asset='USDT', source='bot', description=None,
                          reference_id=None, metadata=None, timestamp=None, ledger_file=DEFAULT_LEDGER_FILE):
    return record_movement(TYPE_REALIZED_PNL, amount, asset, source, description, reference_id, metadata, timestamp, ledger_file)
