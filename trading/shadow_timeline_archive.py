#!/usr/bin/env python3
"""Offline dual-reader comparison for current Timeline consumers.

The monolithic reader is always authoritative. Multi-shard outputs are only
compared and never returned to production callbacks.
"""
import hashlib
import json
import os
from datetime import datetime
from unittest import mock

import archive_manifest
import audit_data_quality
import decision_timeline
import gap_analysis
import telegram_commands
import trade_inspector

EFFECTIVE_SOURCE = 'MONOLITHIC_ONLY'
SHADOW_SOURCE = 'MULTI_SHARD_COMPARISON_ONLY'


def _json_safe(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _digest(value):
    payload = json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def _timeline_text(path):
    original = decision_timeline.read_recent_events
    def shadow_read(limit=20, category=None, symbol=None, **_kwargs):
        return original(limit=limit, category=category, symbol=symbol, path=path)
    with mock.patch.object(telegram_commands.decision_timeline, 'read_recent_events', side_effect=shadow_read):
        return telegram_commands._timeline_text()


def consumer_outputs(path):
    records = list(trade_inspector._iter_jsonl(path) or [])
    normalized = [decision_timeline.normalise_event_schema(row) for row in records]
    operational_intervals = [gap_analysis.evidence_interval(row) for row in normalized]
    operational_intervals = [item for item in operational_intervals if item]
    return _json_safe({
        'recent_all_20': decision_timeline.read_recent_events(limit=20, path=path),
        'recent_operational_10': decision_timeline.read_recent_events(limit=10, category='OPERATIONAL', path=path),
        'recent_diagnostic_10': decision_timeline.read_recent_events(limit=10, category='DIAGNOSTIC', path=path),
        'recent_debug_10': decision_timeline.read_recent_events(limit=10, category='DEBUG', path=path),
        'operational_view': list(decision_timeline.iter_operational_events(path)),
        'diagnostic_view': list(decision_timeline.iter_diagnostic_events(path)),
        'debug_view': list(decision_timeline.iter_debug_events(path)),
        'telegram_operational_text': _timeline_text(path),
        'auditor_pause_windows': audit_data_quality._collect_pause_windows_from_timeline(path),
        'gap_compatible_intervals': operational_intervals,
        'trade_inspector_important_timeline': trade_inspector._important_timeline(records),
    })


def _first_difference(left, right, path='$'):
    if type(left) is not type(right):
        return {'path': path, 'monolithic': type(left).__name__, 'shadow': type(right).__name__}
    if isinstance(left, dict):
        if set(left) != set(right):
            return {'path': path, 'monolithic_keys': sorted(left), 'shadow_keys': sorted(right)}
        for key in sorted(left):
            difference = _first_difference(left[key], right[key], f'{path}.{key}')
            if difference:
                return difference
        return None
    if isinstance(left, list):
        if len(left) != len(right):
            return {'path': path, 'monolithic_length': len(left), 'shadow_length': len(right)}
        for index, (first, second) in enumerate(zip(left, right)):
            difference = _first_difference(first, second, f'{path}[{index}]')
            if difference:
                return difference
        return None
    if left != right:
        return {'path': path, 'monolithic': left, 'shadow': right}
    return None


def compare_outputs(monolithic, shadow):
    consumers = {}
    for name in sorted(set(monolithic) | set(shadow)):
        left, right = monolithic.get(name), shadow.get(name)
        difference = _first_difference(left, right)
        consumers[name] = {
            'equal': difference is None, 'monolithic_digest': _digest(left),
            'shadow_digest': _digest(right), 'first_difference': difference,
        }
    return {'equal': all(item['equal'] for item in consumers.values()), 'consumers': consumers}


def materialize_manifest(manifest_path, project_dir, destination):
    records = list(archive_manifest.iter_records(manifest_path, 'timeline', project_dir=project_dir))
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    with open(destination, 'wb') as handle:
        for record in records:
            handle.write(archive_manifest._canonical(record))
    return {'path': destination, 'record_count': len(records),
            'logical_fingerprint': archive_manifest.logical_fingerprint(
                manifest_path, 'timeline', project_dir=project_dir)}


def compare_layouts(monolithic_path, layouts, output_dir):
    output_real = os.path.realpath(output_dir)
    if os.path.commonpath(('/tmp', output_real)) != '/tmp':
        raise ValueError('dual-reader output must be under /tmp')
    os.makedirs(output_real, exist_ok=True)
    authoritative = consumer_outputs(monolithic_path)
    authoritative_records = list(trade_inspector._iter_jsonl(monolithic_path) or [])
    authoritative_fingerprint = {
        'algorithm': 'sha256-canonical-jsonl-v1', 'record_count': len(authoritative_records),
        'sha256': hashlib.sha256(b''.join(archive_manifest._canonical(row) for row in authoritative_records)).hexdigest(),
    }
    comparisons = {}
    for name, layout in layouts.items():
        destination = os.path.join(output_real, f'{name}-materialized.jsonl')
        materialized = materialize_manifest(layout['manifest_path'], layout['project_dir'], destination)
        outputs = consumer_outputs(destination)
        comparison = compare_outputs(authoritative, outputs)
        comparison.update({'materialized': materialized,
                           'logical_fingerprint_equal': materialized['logical_fingerprint'] == authoritative_fingerprint})
        comparisons[name] = comparison
    return {
        'dual_reader_schema_version': 1, 'effective_source': EFFECTIVE_SOURCE,
        'shadow_source': SHADOW_SOURCE, 'production_result_source': monolithic_path,
        'shadow_results_used_by_runtime': False, 'authoritative_fingerprint': authoritative_fingerprint,
        'authoritative_consumer_digests': {key:_digest(value) for key,value in authoritative.items()},
        'layouts': comparisons,
        'differences_detected': any(not item['equal'] for item in comparisons.values()),
        'valid': all(item['equal'] and item['logical_fingerprint_equal'] for item in comparisons.values()),
    }
