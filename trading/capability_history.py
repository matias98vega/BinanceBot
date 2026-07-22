#!/usr/bin/env python3
"""Canonical read-only registry for functional capabilities and version policy."""

CAPABILITY_SCHEMA_VERSION = 1
CAPABILITY_STATUSES = {'PLANNED', 'IMPLEMENTED', 'DEPLOYED', 'OBSERVING', 'ACTIVE', 'SUPERSEDED', 'RETIRED', 'BLOCKED'}
CHANGE_CLASSES = {'BEHAVIORAL_VERSION_CHANGE', 'NON_BEHAVIORAL_CAPABILITY_CHANGE'}

KNOWN_HISTORICAL_VERSION_CONFLICTS = {
    'short_EWYUSDT_1783476970': {
        'opening_version': 'v1.0-alpha', 'conflicting_event_version': 'v1.1-observability-hardening',
        'canonical_version': 'v1.0-alpha', 'classification': 'KNOWN_IMMUTABLE_HISTORICAL_VERSION_CONFLICT',
        'analytics_membership': 'opening_version',
    },
    'short_SAMSUNGUSDT_1783477221': {
        'opening_version': 'v1.0-alpha', 'conflicting_event_version': 'v1.1-observability-hardening',
        'canonical_version': 'v1.0-alpha', 'classification': 'KNOWN_IMMUTABLE_HISTORICAL_VERSION_CONFLICT',
        'analytics_membership': 'opening_version',
    },
}


def _capability(capability_id, name, status, introduced_at, commit, behavioral=False,
                bot_versions=(), predecessor=None, **effects):
    return {
        'id': capability_id, 'name': name, 'status': status, 'introduced_at': introduced_at,
        'introduced_by_commit': commit, 'behavioral': behavioral,
        'change_class': 'BEHAVIORAL_VERSION_CHANGE' if behavioral else 'NON_BEHAVIORAL_CAPABILITY_CHANGE',
        'affects_trade_selection': bool(effects.get('affects_trade_selection')),
        'affects_trade_management': bool(effects.get('affects_trade_management')),
        'affects_accounting': bool(effects.get('affects_accounting')),
        'affects_observability': bool(effects.get('affects_observability')),
        'affects_ml_dataset': bool(effects.get('affects_ml_dataset')),
        'bot_versions': tuple(bot_versions), 'predecessor': predecessor, 'notes': effects.get('notes', ''),
    }


CAPABILITIES = (
    _capability('core-v1', 'Core trading engine', 'DEPLOYED', '2026-06-01T00:00:00Z', 'be264e3', True,
                ('v1.0-alpha',), affects_trade_selection=True, affects_trade_management=True,
                notes='Historical anchor; start date comes from version_history.'),
    _capability('observability-v1', 'Observability hardening', 'DEPLOYED', '2026-07-08T00:00:00Z', '9df9777',
                affects_observability=True),
    _capability('sizing-v2', 'Sizing v2 exposure model', 'ACTIVE', '2026-07-12T18:04:31Z', 'cd4a5f5', True,
                ('v1.2-sizing-v2',), predecessor='core-v1', affects_trade_selection=True,
                notes='Changes entry quantity and exposure.'),
    _capability('version-metrics-v1', 'Trade metrics by opening version', 'DEPLOYED', '2026-07-20T19:00:34Z',
                '4566c32', affects_observability=True),
    _capability('accounting-v2', 'Capital ledger schema v2 and bootstrap accounting', 'DEPLOYED',
                '2026-07-20T21:24:37Z', '8ded902', affects_accounting=True, affects_observability=True),
    _capability('rebalance-reconciliation-v1', 'Automatic aligned rebalance reconciliation', 'DEPLOYED',
                '2026-07-20T20:35:23Z', '335da04', affects_accounting=True, affects_observability=True),
    _capability('spot-stale-reconciliation-v1', 'Conservative Spot stale reconciliation', 'DEPLOYED',
                '2026-07-20T21:37:58Z', '5fc9fe6', affects_observability=True),
    _capability('fake-exchange-v1', 'FakeBinanceClient end-to-end harness', 'IMPLEMENTED',
                '2026-07-21T17:51:10Z', '1804253'),
    _capability('replay-v1', 'Deterministic offline ReplayClient', 'IMPLEMENTED', '2026-07-21T19:28:50Z', '861c659'),
    _capability('incident-replay-v1', 'Sanitized replay incident library', 'IMPLEMENTED',
                '2026-07-21T19:46:31Z', '86765c1', predecessor='replay-v1'),
    _capability('operational-evidence-v1', 'Operational timeline and gap evidence', 'DEPLOYED',
                '2026-07-21T19:09:41Z', '8d7bcf8', affects_observability=True),
    _capability('preentry-audit-v1', 'Pre-entry safety gate in AUDIT_ONLY', 'OBSERVING',
                '2026-07-21T18:34:01Z', 'a68cc3c', affects_observability=True, affects_ml_dataset=True,
                notes='AUDIT_ONLY records evidence and cannot block entries.'),
    _capability('preentry-evidence-v1', 'Durable pre-entry mismatch evidence', 'OBSERVING',
                '2026-07-22T22:25:57Z', '4546d86', predecessor='preentry-audit-v1',
                affects_observability=True,
                notes='Append-only sanitized evidence and offline tolerance policies; no decision integration.'),
    _capability('feature-capture-v2', 'Passive feature capture schema v2', 'OBSERVING',
                '2026-07-21T02:57:15Z', 'b64fd3f', affects_ml_dataset=True),
    _capability('statistical-baseline-v1', 'Reproducible statistical baseline', 'IMPLEMENTED',
                '2026-07-21T02:08:06Z', '46546ed', affects_ml_dataset=True),
    _capability('xgboost-offline-v1', 'Offline CPU XGBoost experiment', 'IMPLEMENTED',
                '2026-07-21T02:30:02Z', '03999b5', affects_ml_dataset=True,
                notes='No deployed model_version and no trading integration.'),
    _capability('telegram-diagnostics-v1', 'Telegram diagnostics presentation polish', 'DEPLOYED',
                '2026-07-21T20:23:50Z', 'c64ef06', affects_observability=True),
)

# Registry iteration is chronological even though declarations are grouped for readability.
CAPABILITIES = tuple(sorted(CAPABILITIES, key=lambda item: (item['introduced_at'], item['id'])))


def capability_ids():
    return tuple(item['id'] for item in CAPABILITIES)


def requires_bot_version(change):
    """Classify a proposed capability without changing runtime state."""
    if not isinstance(change, dict):
        return False
    if change.get('pre_entry_gate_mode') == 'ENFORCE' or change.get('model_mode') == 'LIVE_FILTER':
        return True
    if change.get('model_mode') == 'SHADOW_READ_ONLY':
        return False
    return any(bool(change.get(key)) for key in ('affects_trade_selection', 'affects_trade_management',
                                                  'affects_sizing', 'affects_risk', 'affects_execution'))
