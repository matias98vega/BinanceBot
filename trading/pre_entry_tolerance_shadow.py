#!/usr/bin/env python3
"""Pure offline policies for pre-entry mismatch evidence; never used by the gate."""
from decimal import Decimal, InvalidOperation

POLICY_VERSION = 'preentry-tolerance-shadow-v1'
POLICIES = ('CURRENT', 'STEP_ONLY', 'RELATIVE_ONLY', 'NOTIONAL_ONLY', 'COMBINED_CONSERVATIVE')
DEFAULTS = {
    'absolute_floor': Decimal('0.000001'),
    'step_multiplier': Decimal('1'),
    'relative_limit': Decimal('0.001'),
    'notional_limit_usdt': Decimal('0.10'),
}


def decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def canonical(value):
    value = decimal(value)
    if value is None:
        return None
    text = format(value.normalize(), 'f')
    return '0' if text in ('-0', '') else text


def _risk_reason(evidence):
    if not evidence.get('exchange_state_complete') or evidence.get('freshness_status') != 'FRESH':
        return 'exchange state incomplete or stale'
    if evidence.get('orphan_detected') or evidence.get('unknown_order_detected'):
        return 'orphan or unknown order state'
    if evidence.get('position_protected') is not True:
        return 'position protection not proven'
    if evidence.get('reconciliation_blocked'):
        return 'material reconciliation block'
    return None


def evaluate_mismatch(evidence, policy_id='COMBINED_CONSERVATIVE', thresholds=None):
    if policy_id not in POLICIES:
        raise ValueError(f'unknown policy: {policy_id}')
    limits = dict(DEFAULTS)
    for key, value in (thresholds or {}).items():
        converted = decimal(value)
        if converted is not None:
            limits[key] = converted
    absolute = decimal(evidence.get('absolute_difference'))
    relative = decimal(evidence.get('relative_difference'))
    notional = decimal(evidence.get('difference_notional'))
    step = decimal(evidence.get('step_size'))
    current_tolerance = decimal(evidence.get('current_quantity_tolerance')) or DEFAULTS['absolute_floor']
    risk = _risk_reason(evidence)
    base = {
        'policy_id': policy_id, 'policy_version': POLICY_VERSION,
        'thresholds': {key: canonical(value) for key, value in limits.items()},
        'material_risk_preserved': True,
    }
    if absolute is None or relative is None:
        return {**base, 'would_pass': False, 'classification': 'SHADOW_FAIL_CLOSED', 'reason': 'invalid quantities'}
    if risk:
        return {**base, 'would_pass': False, 'classification': 'SHADOW_FAIL_CLOSED', 'reason': risk}
    if policy_id == 'CURRENT':
        passed = absolute <= current_tolerance
        return {**base, 'would_pass': passed, 'classification': 'CURRENT_PASS' if passed else 'CURRENT_BLOCK',
                'reason': 'current raw quantity tolerance'}
    if policy_id in ('STEP_ONLY', 'COMBINED_CONSERVATIVE') and (not evidence.get('filters_available') or step is None):
        return {**base, 'would_pass': False, 'classification': 'SHADOW_INSUFFICIENT_FILTERS', 'reason': 'stepSize unavailable'}
    if policy_id in ('NOTIONAL_ONLY', 'COMBINED_CONSERVATIVE') and notional is None:
        return {**base, 'would_pass': False, 'classification': 'SHADOW_FAIL_CLOSED', 'reason': 'notional unavailable'}
    checks = {
        'STEP_ONLY': absolute <= max(limits['step_multiplier'] * step, limits['absolute_floor']) if step is not None else False,
        'RELATIVE_ONLY': relative <= limits['relative_limit'],
        'NOTIONAL_ONLY': notional <= limits['notional_limit_usdt'] if notional is not None else False,
    }
    if policy_id == 'COMBINED_CONSERVATIVE':
        passed = checks['STEP_ONLY'] and checks['RELATIVE_ONLY'] and checks['NOTIONAL_ONLY']
    else:
        passed = checks[policy_id]
    return {**base, 'would_pass': passed,
            'classification': 'SHADOW_TOLERATED_DUST' if passed else 'SHADOW_MATERIAL_MISMATCH',
            'reason': 'candidate thresholds satisfied' if passed else 'one or more candidate thresholds exceeded'}


def evaluate_all(evidence):
    return [evaluate_mismatch(evidence, policy) for policy in POLICIES]
