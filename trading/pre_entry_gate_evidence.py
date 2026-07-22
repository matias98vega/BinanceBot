#!/usr/bin/env python3
"""Durable, fail-open observability for pre-entry gate evaluations."""
import fcntl
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from decimal import Decimal

import pre_entry_tolerance_shadow
import version_history

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(PROJECT_DIR, 'data', 'history', 'pre_entry_gate_evidence.jsonl')
EVIDENCE_SCHEMA_VERSION = 1
EVIDENCE_CAPTURE_VERSION = 'preentry-mismatch-evidence-v1'
TAIL_BYTES = 131072


def _iso(value=None):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    if value:
        return str(value)
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _canonical_decimal(value):
    return pre_entry_tolerance_shadow.canonical(value)


def evaluation_id(result, cycle_id):
    stable = '|'.join(str(value or '') for value in (
        cycle_id, result.get('side'), result.get('symbol'), result.get('mode'), result.get('observed_at')))
    return 'peg_' + hashlib.sha256(stable.encode('utf-8')).hexdigest()[:24]


def _check(result, name):
    return ((result.get('checks') or {}).get(name) or {})


def _cached_metadata(context, symbol, side):
    metadata = ((context or {}).get('symbol_metadata') or {}).get(symbol) or {}
    return metadata.get(str(side).upper()) or metadata.get('SPOT') or metadata.get('FUTURES') or metadata


def _mismatch_record(item, result, local_state, context):
    symbol = str(item.get('symbol') or '').upper()
    local = pre_entry_tolerance_shadow.decimal(item.get('local'))
    exchange = pre_entry_tolerance_shadow.decimal(item.get('exchange'))
    absolute = abs(exchange - local) if local is not None and exchange is not None else None
    relative = absolute / abs(local) if absolute is not None and local not in (None, Decimal('0')) else None
    side = 'SHORT' if local is not None and local < 0 else 'LONG'
    price = pre_entry_tolerance_shadow.decimal(((context or {}).get('mark_prices') or {}).get(symbol))
    metadata = _cached_metadata(context, symbol, side)
    step = pre_entry_tolerance_shadow.decimal(metadata.get('step_size'))
    expected = abs(local) if local is not None else None
    qty_tolerance = pre_entry_tolerance_shadow.decimal((result.get('tolerances') or {}).get('quantity'))
    effective = max(qty_tolerance or Decimal('0'), (expected or Decimal('0')) * (qty_tolerance or Decimal('0')))
    positions = [p for p in (local_state.get('positions') or []) if str(p.get('symbol') or '').upper() == symbol]
    protected_check = _check(result, 'EXISTING_POSITIONS_PROTECTED')
    evidence = {
        'symbol': symbol, 'position_side': side,
        'local_quantity': _canonical_decimal(local), 'exchange_quantity': _canonical_decimal(exchange),
        'absolute_difference': _canonical_decimal(absolute), 'relative_difference': _canonical_decimal(relative),
        'mark_price': _canonical_decimal(price),
        'difference_notional': _canonical_decimal(absolute * price if absolute is not None and price is not None else None),
        'local_notional': _canonical_decimal(abs(local) * price if local is not None and price is not None else None),
        'exchange_notional': _canonical_decimal(abs(exchange) * price if exchange is not None and price is not None else None),
        'step_size': _canonical_decimal(step), 'min_qty': _canonical_decimal(metadata.get('min_qty')),
        'max_qty': _canonical_decimal(metadata.get('max_qty')), 'min_notional': _canonical_decimal(metadata.get('min_notional')),
        'quantity_precision': metadata.get('quantity_precision'), 'price_precision': metadata.get('price_precision'),
        'tick_size': _canonical_decimal(metadata.get('tick_size')),
        'filters_available': bool(metadata.get('filters_available') and step is not None),
        'filters_source': metadata.get('filters_source'),
        'below_step_size': absolute <= step if absolute is not None and step is not None else None,
        'below_min_qty': absolute < pre_entry_tolerance_shadow.decimal(metadata.get('min_qty')) if absolute is not None and pre_entry_tolerance_shadow.decimal(metadata.get('min_qty')) is not None else None,
        'below_min_notional': absolute * price < pre_entry_tolerance_shadow.decimal(metadata.get('min_notional')) if absolute is not None and price is not None and pre_entry_tolerance_shadow.decimal(metadata.get('min_notional')) is not None else None,
        'position_managed': bool(positions), 'position_protected': protected_check.get('passed') is True,
        'protection_type': 'OCO' if side == 'LONG' and protected_check.get('passed') is True else 'REDUCE_ONLY' if side == 'SHORT' and protected_check.get('passed') is True else None,
        'current_quantity_tolerance': _canonical_decimal(effective),
    }
    evidence.update({
        'exchange_state_complete': _check(result, 'EXCHANGE_READ_COMPLETE').get('passed') is True,
        'freshness_status': (result.get('freshness') or {}).get('exchange'),
        'orphan_detected': _check(result, 'NO_ORPHAN_POSITIONS').get('passed') is False,
        'unknown_order_detected': _check(result, 'NO_UNKNOWN_ORDER_STATE').get('passed') is False,
        'reconciliation_blocked': _check(result, 'NO_PENDING_RECONCILIATION').get('passed') is False,
    })
    evidence['shadow_policies'] = pre_entry_tolerance_shadow.evaluate_all(evidence)
    return evidence


def build_evaluation(result, local_state, cycle_id=None, context=None):
    mismatches = (_check(result, 'MANAGED_POSITIONS_MATCH_OBSERVED').get('evidence') or {}).get('mismatches') or []
    capacity = _check(result, 'CAPACITY_AVAILABLE')
    capacity_evidence = capacity.get('evidence') or {}
    reconciliation = _check(result, 'NO_PENDING_RECONCILIATION')
    orphan = _check(result, 'NO_ORPHAN_POSITIONS').get('passed') is False
    unknown = _check(result, 'NO_UNKNOWN_ORDER_STATE').get('passed') is False
    protected = _check(result, 'EXISTING_POSITIONS_PROTECTED').get('passed') is True
    exchange_complete = _check(result, 'EXCHANGE_READ_COMPLETE').get('passed') is True
    local_valid = _check(result, 'LOCAL_STATE_VALID').get('passed') is True
    relevant = capacity.get('passed') is True and reconciliation.get('passed') is True
    record = {
        'evidence_schema_version': EVIDENCE_SCHEMA_VERSION,
        'evidence_capture_version': EVIDENCE_CAPTURE_VERSION,
        'event_type': 'GATE_EVALUATION', 'timestamp': _iso(), 'cycle_id': cycle_id,
        'evaluation_id': evaluation_id(result, cycle_id), 'bot_version': version_history.current_version(),
        'strategy_version': version_history.STRATEGY_VERSION,
        'capability_epochs': ['preentry-audit-v1', 'preentry-evidence-v1'],
        'gate_mode': result.get('mode'), 'gate_status': result.get('status'),
        'safe_to_enter': result.get('safe_to_enter'), 'entry_allowed': result.get('entry_allowed'),
        'side': result.get('side'), 'candidate_symbol': result.get('symbol'),
        'reason_codes': result.get('blocking_reasons') or [],
        'local_state_timestamp': (local_state or {}).get('updated_at'),
        'exchange_state_timestamp': result.get('observed_at'),
        'freshness_seconds': _canonical_decimal((result.get('freshness') or {}).get('age_seconds')),
        'freshness_status': (result.get('freshness') or {}).get('exchange'),
        'evaluation_duration_ms': _canonical_decimal(result.get('duration_ms')),
        'exchange_state_complete': exchange_complete, 'local_state_valid': local_valid,
        'data_source': result.get('source'), 'fallback_used': False,
        'mismatches': [_mismatch_record(item, result, local_state or {}, context or {}) for item in mismatches],
        'orphan_detected': orphan, 'unknown_order_detected': unknown,
        'position_protected': protected,
        'reconciliation_blocked': reconciliation.get('passed') is False,
        'reconciliation_reason': reconciliation.get('reason') if reconciliation.get('passed') is False else None,
        'capacity_blocked': capacity.get('passed') is False, 'capacity_side': result.get('side'),
        'current_positions': capacity_evidence.get('current'), 'operational_max': capacity_evidence.get('operational_max'),
        'target_max': capacity_evidence.get('target_max'), 'new_entries_allowed': capacity_evidence.get('new_entries_allowed'),
        'material_risk_detected': bool(orphan or unknown or not protected or reconciliation.get('passed') is False or not exchange_complete),
        'candidate_reached_pre_entry': True, 'gate_evaluation_completed': True,
        'enforce_relevant_evaluation': bool(relevant),
        'trade_opened_after_evaluation': None, 'opened_trade_id': None, 'opened_timestamp': None,
        'seconds_to_open': None, 'opened_same_symbol': None, 'opened_same_side': None,
    }
    return record


def build_outcome(result, position, cycle_id=None, opened_at=None):
    opened = isinstance(position, dict) and bool(position.get('id'))
    evaluation_ts = result.get('observed_at')
    opened_at = opened_at or (_iso(position.get('entry_time')) if opened and isinstance(position.get('entry_time'), str) else _iso() if opened else None)
    seconds = None
    try:
        start = datetime.fromisoformat(str(evaluation_ts).replace('Z', '+00:00')).timestamp()
        end = datetime.fromisoformat(str(opened_at).replace('Z', '+00:00')).timestamp()
        seconds = max(0, end - start)
    except Exception:
        pass
    return {
        'evidence_schema_version': EVIDENCE_SCHEMA_VERSION, 'evidence_capture_version': EVIDENCE_CAPTURE_VERSION,
        'event_type': 'GATE_ENTRY_OUTCOME', 'timestamp': _iso(), 'cycle_id': cycle_id,
        'bot_version': version_history.current_version(), 'strategy_version': version_history.STRATEGY_VERSION,
        'evaluation_id': evaluation_id(result, cycle_id), 'trade_opened': opened,
        'trade_id': position.get('id') if opened else None,
        'symbol': position.get('symbol') if opened else result.get('symbol'),
        'side': str(position.get('direction') or '').upper() if opened else result.get('side'),
        'opened_at': opened_at, 'seconds_after_evaluation': _canonical_decimal(seconds),
        'same_symbol': str(position.get('symbol') or '').upper() == str(result.get('symbol') or '').upper() if opened else None,
        'same_side': str(position.get('direction') or '').upper() == str(result.get('side') or '').upper() if opened else None,
    }


def _tail_contains(handle, evaluation, event_type):
    handle.seek(0, os.SEEK_END); size = handle.tell(); handle.seek(max(0, size - TAIL_BYTES))
    tail = handle.read().decode('utf-8', errors='ignore')
    marker_id = f'"evaluation_id":"{evaluation}"'
    marker_type = f'"event_type":"{event_type}"'
    return any(marker_id in line and marker_type in line for line in tail.splitlines())


def append_record(record, path=None):
    path = path or os.getenv('PRE_ENTRY_GATE_EVIDENCE_PATH') or DEFAULT_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = (json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
    started = time.perf_counter()
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_RDWR, 0o640)
    try:
        with os.fdopen(fd, 'r+b', closefd=False) as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            if _tail_contains(handle, record.get('evaluation_id'), record.get('event_type')):
                return {'written': False, 'duplicate': True, 'duration_ms': (time.perf_counter()-started)*1000, 'bytes': 0}
            os.write(fd, payload); os.fsync(fd)
            return {'written': True, 'duplicate': False, 'duration_ms': (time.perf_counter()-started)*1000, 'bytes': len(payload)}
    finally:
        try: fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception: pass
        os.close(fd)


def capture_evaluation(result, local_state, cycle_id=None, context=None, path=None):
    try:
        return append_record(build_evaluation(result, local_state, cycle_id, context), path)
    except Exception as exc:
        logging.warning('pre-entry evidence persistence failed: %s', exc)
        return {'written': False, 'error': str(exc)}


def capture_outcome(result, position, cycle_id=None, path=None):
    try:
        return append_record(build_outcome(result, position, cycle_id), path)
    except Exception as exc:
        logging.warning('pre-entry outcome persistence failed: %s', exc)
        return {'written': False, 'error': str(exc)}


def cached_observation_context(local_state, mark_prices=None):
    """Read process-local filter caches only; this function never fetches."""
    try:
        import utils
    except Exception:
        return {'mark_prices': dict(mark_prices or {}), 'symbol_metadata': {}}
    metadata = {}
    for position in (local_state or {}).get('positions', []):
        symbol = str(position.get('symbol') or '').upper()
        side = str(position.get('direction') or '').upper()
        row = {}
        if side == 'LONG':
            raw = getattr(utils, '_spot_info_cache', {}).get(symbol)
            try:
                info = raw.get('symbols', [])[0]
                filters = {item.get('filterType'): item for item in info.get('filters', [])}
                lot = filters.get('LOT_SIZE') or {}
                notion = filters.get('NOTIONAL') or filters.get('MIN_NOTIONAL') or {}
                price_filter = filters.get('PRICE_FILTER') or {}
                row = {
                    'step_size': lot.get('stepSize'), 'min_qty': lot.get('minQty'), 'max_qty': lot.get('maxQty'),
                    'min_notional': notion.get('minNotional') or notion.get('notional'),
                    'quantity_precision': info.get('baseAssetPrecision'),
                    'price_precision': info.get('quotePrecision'), 'tick_size': price_filter.get('tickSize'),
                    'filters_available': True, 'filters_source': 'process_cache_spot_exchange_info',
                }
            except Exception:
                row = {}
        else:
            raw = getattr(utils, '_futures_info_cache', {}).get(symbol)
            if isinstance(raw, dict):
                row = {**raw, 'filters_available': bool(raw.get('step_size')),
                       'filters_source': 'process_cache_futures_exchange_info'}
        metadata[symbol] = {side: row}
    return {'mark_prices': dict(mark_prices or {}), 'symbol_metadata': metadata}
