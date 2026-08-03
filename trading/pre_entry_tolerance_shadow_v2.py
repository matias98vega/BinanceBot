#!/usr/bin/env python3
"""Pure, deterministic step/dust shadow policy; never used by the live gate.

The module consumes explicit persisted evidence only.  It has no client, file,
environment, clock, state or network dependency and cannot affect CURRENT,
``safe_to_enter`` or ``entry_allowed``.
"""
from decimal import Decimal, InvalidOperation


POLICY_VERSION = 'preentry-tolerance-shadow-v2'
ABSOLUTE_NOTIONAL_CAP_USDT = Decimal('0.50')
STEP_MULTIPLIER = Decimal('1.0')

SAFE_EXACT_MATCH = 'SAFE_EXACT_MATCH'
SAFE_NON_OPERABLE_DUST = 'SAFE_NON_OPERABLE_DUST'
BLOCKED = 'BLOCKED'

REASON_ORDER = (
    'BLOCK_INCOMPLETE_EVIDENCE',
    'BLOCK_ORPHAN',
    'BLOCK_UNKNOWN_ORDER',
    'BLOCK_UNMANAGED_POSITION',
    'BLOCK_UNPROTECTED',
    'BLOCK_RECONCILIATION_RISK',
    'BLOCK_SIDE_MISMATCH',
    'BLOCK_UNVALIDATED_SIDE',
    'BLOCK_INVALID_FILTERS',
    'BLOCK_STALE_PRICE',
    'BLOCK_STEP_BOUND',
    'BLOCK_OPERABLE_QUANTITY',
    'BLOCK_OPERABLE_NOTIONAL',
    'BLOCK_ABSOLUTE_NOTIONAL_CAP',
    'BLOCK_ACCUMULATED_DUST',
    'BLOCK_CURRENT_GATE_REASON',
    SAFE_EXACT_MATCH,
    SAFE_NON_OPERABLE_DUST,
)
_REASON_INDEX = {reason: index for index, reason in enumerate(REASON_ORDER)}


def decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def canonical(value):
    value = decimal(value)
    if value is None:
        return None
    text = format(value.normalize(), 'f')
    return '0' if text in ('', '-0') else text


def _ordered(reasons):
    return sorted(set(reasons), key=lambda reason: (_REASON_INDEX.get(reason, len(REASON_ORDER)), reason))


def _normalized_price_sources(evidence):
    raw_sources = evidence.get('price_sources')
    if not isinstance(raw_sources, list):
        raw_sources = []
    if not raw_sources and evidence.get('mark_price') is not None:
        raw_sources = [{
            'source': evidence.get('price_source') or 'POSITION_MANAGEMENT_CURRENT_PRICE',
            'price': evidence.get('mark_price'),
            'timestamp': evidence.get('price_timestamp') or evidence.get('exchange_state_timestamp'),
            'age_seconds': evidence.get('price_age_seconds', evidence.get('freshness_seconds')),
            'fresh': evidence.get('price_fresh', evidence.get('freshness_status') == 'FRESH'),
        }]
    normalized = []
    seen = set()
    for item in raw_sources:
        if not isinstance(item, dict):
            continue
        price = decimal(item.get('price'))
        source = str(item.get('source') or 'UNKNOWN')
        timestamp = item.get('timestamp') or item.get('observed_at')
        age = decimal(item.get('age_seconds'))
        fresh = item.get('fresh') is True
        key = (source, str(timestamp or ''), canonical(price))
        if key in seen:
            continue
        seen.add(key)
        normalized.append({
            'source': source,
            'price': canonical(price),
            'timestamp': timestamp,
            'age_seconds': canonical(age),
            'fresh': fresh,
            'valid': bool(price is not None and price > 0 and fresh and timestamp),
        })
    return normalized


def _accumulation(evidence, difference):
    raw = evidence.get('accumulated_dust')
    raw = raw if isinstance(raw, dict) else {}
    return {
        'first_seen_at': raw.get('first_seen_at'),
        'last_seen_at': raw.get('last_seen_at'),
        'observation_count': raw.get('observation_count'),
        'current_difference': canonical(raw.get('current_difference')) or canonical(difference),
        'max_observed_difference': canonical(raw.get('max_observed_difference')),
        'reconciliation_count': raw.get('reconciliation_count'),
        'blocked': raw.get('blocked') is True,
    }


def evaluate_mismatch(evidence):
    """Evaluate one mismatch snapshot and return a fully serializable result."""
    evidence = dict(evidence or {})
    local = decimal(evidence.get('local_quantity'))
    exchange = decimal(evidence.get('exchange_quantity'))
    difference = abs(exchange - local) if local is not None and exchange is not None else None
    supplied_difference = decimal(evidence.get('absolute_difference'))
    step = decimal(evidence.get('step_size'))
    min_qty = decimal(evidence.get('min_qty'))
    minimum_notional = decimal(evidence.get('min_notional') if evidence.get('min_notional') is not None
                               else evidence.get('minimum_notional'))
    persisted_notional = decimal(evidence.get('difference_notional'))
    sources = _normalized_price_sources(evidence)
    valid_sources = [item for item in sources if item['valid']]
    selected_source = max(valid_sources, key=lambda item: decimal(item['price'])) if valid_sources else None
    selected_price = decimal(selected_source['price']) if selected_source else None
    calculated_notional = difference * selected_price if difference is not None and selected_price is not None else None
    notional_values = [value for value in (persisted_notional, calculated_notional)
                       if value is not None and value >= 0]
    conservative_notional = max(notional_values) if notional_values else None
    difference_steps = difference / step if difference is not None and step not in (None, Decimal('0')) else None
    position_side = str(evidence.get('position_side') or '').upper()
    candidate_side = str(evidence.get('candidate_side') or '').upper()
    accumulation = _accumulation(evidence, difference)
    reasons = []

    if (evidence.get('exchange_state_complete') is not True
            or evidence.get('fallback_used') is True
            or candidate_side not in {'LONG', 'SHORT'}
            or local is None or exchange is None
            or (evidence.get('absolute_difference') is not None
                and (supplied_difference is None or supplied_difference < 0))
            or (evidence.get('difference_notional') is not None
                and (persisted_notional is None or persisted_notional < 0))):
        reasons.append('BLOCK_INCOMPLETE_EVIDENCE')
    if evidence.get('orphan_detected') is True:
        reasons.append('BLOCK_ORPHAN')
    if evidence.get('unknown_order_detected') is True:
        reasons.append('BLOCK_UNKNOWN_ORDER')
    if evidence.get('position_managed') is not True:
        reasons.append('BLOCK_UNMANAGED_POSITION')
    if evidence.get('position_protected') is not True:
        reasons.append('BLOCK_UNPROTECTED')
    if evidence.get('reconciliation_blocked') is True or evidence.get('reconciliation_risk') is True:
        reasons.append('BLOCK_RECONCILIATION_RISK')

    if position_side not in {'LONG', 'SHORT'}:
        reasons.append('BLOCK_SIDE_MISMATCH')
    elif local is not None and exchange is not None:
        same_nonzero_sign = local != 0 and exchange != 0 and (local > 0) == (exchange > 0)
        side_matches = ((position_side == 'LONG' and local > 0 and exchange > 0)
                        or (position_side == 'SHORT' and local < 0 and exchange < 0))
        if not same_nonzero_sign or not side_matches or evidence.get('side_ambiguous') is True:
            reasons.append('BLOCK_SIDE_MISMATCH')
    if evidence.get('multiple_positions_same_symbol') is True:
        reasons.append('BLOCK_SIDE_MISMATCH')
    if position_side == 'SHORT':
        reasons.append('BLOCK_UNVALIDATED_SIDE')

    exact_match = difference == 0 if difference is not None else False
    if not exact_match:
        if difference is None or difference <= 0:
            reasons.append('BLOCK_INCOMPLETE_EVIDENCE')
        if (evidence.get('filters_available') is not True or step is None or step <= 0
                or min_qty is None or min_qty <= 0
                or minimum_notional is None or minimum_notional <= 0):
            reasons.append('BLOCK_INVALID_FILTERS')
        if not valid_sources or conservative_notional is None:
            reasons.append('BLOCK_STALE_PRICE')
        if difference is not None and step is not None and step > 0 and difference >= STEP_MULTIPLIER * step:
            reasons.append('BLOCK_STEP_BOUND')
        if difference is not None and min_qty is not None and min_qty > 0 and difference >= min_qty:
            reasons.append('BLOCK_OPERABLE_QUANTITY')
        if (conservative_notional is not None and minimum_notional is not None
                and minimum_notional > 0 and conservative_notional >= minimum_notional):
            reasons.append('BLOCK_OPERABLE_NOTIONAL')
        if conservative_notional is not None and conservative_notional > ABSOLUTE_NOTIONAL_CAP_USDT:
            reasons.append('BLOCK_ABSOLUTE_NOTIONAL_CAP')
        if accumulation['blocked']:
            reasons.append('BLOCK_ACCUMULATED_DUST')

    reasons = _ordered(reasons)
    if reasons:
        decision = BLOCKED
        primary = reasons[0]
    else:
        decision = SAFE_EXACT_MATCH if exact_match else SAFE_NON_OPERABLE_DUST
        reasons = [decision]
        primary = decision

    return {
        'policy_version': POLICY_VERSION,
        'decision': decision,
        'primary_reason': primary,
        'reason_codes': reasons,
        'symbol': evidence.get('symbol'),
        'position_side': position_side or None,
        'candidate_side': candidate_side or None,
        'local_quantity': canonical(local),
        'exchange_quantity': canonical(exchange),
        'signed_local_quantity': canonical(local),
        'signed_exchange_quantity': canonical(exchange),
        'difference_quantity': canonical(difference),
        'difference_steps': canonical(difference_steps),
        'step_size': canonical(step),
        'min_qty': canonical(min_qty),
        'minimum_notional': canonical(minimum_notional),
        'persisted_difference_notional': canonical(persisted_notional),
        'calculated_difference_notional': canonical(calculated_notional),
        'conservative_difference_notional': canonical(conservative_notional),
        'absolute_notional_cap': canonical(ABSOLUTE_NOTIONAL_CAP_USDT),
        'step_multiplier': canonical(STEP_MULTIPLIER),
        'price_sources': sources,
        'selected_price': selected_source['price'] if selected_source else None,
        'selected_price_source': selected_source['source'] if selected_source else None,
        'selected_price_timestamp': selected_source['timestamp'] if selected_source else None,
        'price_age_seconds': selected_source['age_seconds'] if selected_source else None,
        'price_fresh': selected_source['fresh'] if selected_source else False,
        'freshness_status': evidence.get('freshness_status'),
        'exchange_state_complete': evidence.get('exchange_state_complete') is True,
        'filters_available': evidence.get('filters_available') is True,
        'managed': evidence.get('position_managed') is True,
        'protected': evidence.get('position_protected') is True,
        'orphan': evidence.get('orphan_detected') is True,
        'unknown_order': evidence.get('unknown_order_detected') is True,
        'reconciliation_risk': bool(evidence.get('reconciliation_blocked') or evidence.get('reconciliation_risk')),
        'fallback_used': evidence.get('fallback_used') is True,
        'evidence_complete': not any(reason in reasons for reason in (
            'BLOCK_INCOMPLETE_EVIDENCE', 'BLOCK_INVALID_FILTERS', 'BLOCK_STALE_PRICE',
        )),
        'accumulated_dust': accumulation,
    }


def evaluate_evaluation(evidence):
    """Aggregate mismatch decisions without feeding the production gate."""
    evidence = dict(evidence or {})
    mismatches = []
    for item in evidence.get('mismatches') or []:
        snapshot = dict(item)
        snapshot.setdefault('candidate_side', evidence.get('candidate_side'))
        snapshot.setdefault('fallback_used', evidence.get('fallback_used', False))
        mismatches.append(evaluate_mismatch(snapshot))

    preserved = sorted(set(evidence.get('current_reason_codes') or []) - {'BLOCKED_POSITION_MISMATCH'})
    aggregate = [reason for item in mismatches if item['decision'] == BLOCKED for reason in item['reason_codes']]
    if preserved:
        aggregate.append('BLOCK_CURRENT_GATE_REASON')
    aggregate = _ordered(aggregate)
    if aggregate:
        decision, primary = BLOCKED, aggregate[0]
    elif not mismatches:
        decision = SAFE_EXACT_MATCH if evidence.get('current_safe_to_enter') is True else BLOCKED
        primary = SAFE_EXACT_MATCH if decision == SAFE_EXACT_MATCH else 'BLOCK_CURRENT_GATE_REASON'
        aggregate = [primary]
    elif all(item['decision'] in {SAFE_EXACT_MATCH, SAFE_NON_OPERABLE_DUST} for item in mismatches):
        decision = SAFE_EXACT_MATCH if all(item['decision'] == SAFE_EXACT_MATCH for item in mismatches) else SAFE_NON_OPERABLE_DUST
        primary = decision
        aggregate = [decision]
    else:  # Defensive: every non-safe mismatch should already contribute a reason.
        decision, primary, aggregate = BLOCKED, 'BLOCK_INCOMPLETE_EVIDENCE', ['BLOCK_INCOMPLETE_EVIDENCE']

    return {
        'policy_version': POLICY_VERSION,
        'current_decision': evidence.get('current_decision'),
        'decision': decision,
        'primary_reason': primary,
        'reason_codes': aggregate,
        'preserved_current_reasons': preserved,
        'mismatches': mismatches,
        'summary': {
            'mismatch_count': len(mismatches),
            'safe_mismatch_count': sum(item['decision'] != BLOCKED for item in mismatches),
            'blocked_mismatch_count': sum(item['decision'] == BLOCKED for item in mismatches),
            'all_mismatches_safe': bool(mismatches) and all(item['decision'] != BLOCKED for item in mismatches),
        },
    }


def evaluate_evidence_record(record):
    """Replay an additive schema-v1 evidence record, including legacy rows."""
    record = dict(record or {})
    mismatches = []
    for item in record.get('mismatches') or []:
        snapshot = dict(item)
        snapshot.update({
            'candidate_side': record.get('side'),
            'fallback_used': record.get('fallback_used', False),
            'exchange_state_timestamp': record.get('exchange_state_timestamp'),
            'freshness_seconds': record.get('freshness_seconds'),
        })
        mismatches.append(snapshot)
    return evaluate_evaluation({
        'current_decision': record.get('gate_status'),
        'current_safe_to_enter': record.get('safe_to_enter'),
        'current_reason_codes': record.get('reason_codes') or [],
        'candidate_side': record.get('side'),
        'fallback_used': record.get('fallback_used', False),
        'mismatches': mismatches,
    })
