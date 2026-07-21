#!/usr/bin/env python3
"""Read-only multi-shard JSONL datasets and versioned archive manifests.

This module is deliberately opt-in. It never creates, moves, compresses,
rotates or truncates files and is not imported by the production writers.
"""
import gzip
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone

MANIFEST_VERSION = 1
FORMATS = {'jsonl', 'jsonl.gz'}
DEFAULT_TIMESTAMP_FIELDS = (
    'timestamp', 'recorded_at', 'occurred_at', 'observed_at', 'completed_at',
    'generated_at', 'opened_at', 'closed_at', 'entry_time', 'exit_time',
)
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _issue(code, message, severity='ERROR', **details):
    return {'code': code, 'severity': severity, 'message': message, **details}


def _time(value):
    if value in (None, ''):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _record_time(record, fields):
    if not isinstance(record, dict):
        return None
    for field in fields:
        parsed = _time(record.get(field))
        if parsed is not None:
            return parsed
    return None


def _canonical(record):
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8') + b'\n'


def _safe_path(project_dir, value):
    if not isinstance(value, str) or not value or os.path.isabs(value):
        raise ValueError('manifest paths must be non-empty project-relative paths')
    root = os.path.realpath(project_dir)
    path = os.path.realpath(os.path.join(root, value))
    if os.path.commonpath((root, path)) != root:
        raise ValueError('manifest path escapes project directory')
    return path


def load_manifest(path, allow_missing=True):
    if not os.path.exists(path):
        if allow_missing:
            return {'manifest_version': MANIFEST_VERSION, 'generated_at': None, 'datasets': {}, '_configured': False}
        raise FileNotFoundError(path)
    with open(path, encoding='utf-8') as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError('archive manifest must be a JSON object')
    payload = dict(payload)
    payload['_configured'] = True
    return payload


def dataset_spec(manifest, dataset, active_path=None):
    configured = (manifest.get('datasets') or {}).get(dataset)
    if configured is None:
        if not active_path:
            raise KeyError(f'dataset not configured: {dataset}')
        return {
            'logical_dataset': dataset, 'active_path': active_path,
            'ordering': 'event_time_then_shard_order', 'deduplication_key': None,
            'timestamp_fields': list(DEFAULT_TIMESTAMP_FIELDS), 'declared_gaps': [], 'shards': [],
        }
    result = dict(configured)
    result.setdefault('logical_dataset', dataset)
    result.setdefault('active_path', active_path)
    result.setdefault('ordering', 'event_time_then_shard_order')
    result.setdefault('deduplication_key', None)
    result.setdefault('timestamp_fields', list(DEFAULT_TIMESTAMP_FIELDS))
    result.setdefault('declared_gaps', [])
    result.setdefault('shards', [])
    return result


def source_specs(manifest_path, dataset, active_path=None):
    manifest = load_manifest(manifest_path)
    spec = dataset_spec(manifest, dataset, active_path)
    sources = [dict(item, source_kind='shard') for item in sorted(
        spec.get('shards') or [], key=lambda item: (item.get('sequence', 0), item.get('path', '')))]
    if spec.get('active_path'):
        sources.append({'path': spec['active_path'], 'format': 'jsonl', 'source_kind': 'active',
                        'sequence': len(sources) + 1, 'closed': False})
    return manifest, spec, sources


@contextmanager
def open_source(path, source_format=None):
    source_format = source_format or ('jsonl.gz' if str(path).endswith('.gz') else 'jsonl')
    if source_format not in FORMATS:
        raise ValueError(f'unsupported shard format: {source_format}')
    opener = gzip.open if source_format == 'jsonl.gz' else open
    with opener(path, 'rt', encoding='utf-8') as handle:
        yield handle


def iter_records(manifest_path, dataset, active_path=None, project_dir=None, include_source=False):
    project_dir = project_dir or PROJECT_DIR
    _manifest, _spec, sources = source_specs(manifest_path, dataset, active_path)
    for source in sources:
        path = _safe_path(project_dir, source['path'])
        if not os.path.exists(path):
            if source['source_kind'] == 'active' and not source.get('required', False):
                continue
            raise FileNotFoundError(path)
        with open_source(path, source.get('format')) as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError(f'{source["path"]}:{line_number}: record is not an object')
                if include_source:
                    yield record, {'path': source['path'], 'line': line_number,
                                   'source_kind': source['source_kind'], 'sequence': source.get('sequence')}
                else:
                    yield record


def logical_fingerprint(manifest_path, dataset, active_path=None, project_dir=None):
    digest = hashlib.sha256()
    count = 0
    for record in iter_records(manifest_path, dataset, active_path, project_dir):
        digest.update(_canonical(record))
        count += 1
    return {'algorithm': 'sha256-canonical-jsonl-v1', 'sha256': digest.hexdigest(), 'record_count': count}


def _physical_metrics(path, source_format):
    physical = hashlib.sha256()
    with open(path, 'rb') as raw:
        while True:
            chunk = raw.read(1024 * 1024)
            if not chunk:
                break
            physical.update(chunk)
    uncompressed = 0
    with (gzip.open(path, 'rb') if source_format == 'jsonl.gz' else open(path, 'rb')) as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            uncompressed += len(chunk)
    return physical.hexdigest(), os.path.getsize(path), uncompressed


def _dedupe_value(record, key):
    if key in (None, ''):
        return hashlib.sha256(_canonical(record)).hexdigest()
    fields = [key] if isinstance(key, str) else list(key)
    values = []
    for field in fields:
        value = record
        for part in str(field).split('.'):
            value = value.get(part) if isinstance(value, dict) else None
        values.append(value)
    return json.dumps(values, sort_keys=True, separators=(',', ':'))


def validate_manifest(manifest_path, project_dir=None, datasets=None, active_paths=None, verify_checksums=True):
    project_dir = project_dir or PROJECT_DIR
    active_paths = active_paths or {}
    errors, warnings, details = [], [], {}
    try:
        manifest = load_manifest(manifest_path)
    except Exception as exc:
        return {'valid': False, 'configured': os.path.exists(manifest_path), 'errors': [_issue('INVALID_MANIFEST_JSON', str(exc))],
                'warnings': [], 'datasets': {}}
    configured = bool(manifest.pop('_configured', False))
    if configured and manifest.get('manifest_version') != MANIFEST_VERSION:
        errors.append(_issue('UNSUPPORTED_MANIFEST_VERSION', f'expected {MANIFEST_VERSION}'))
    if configured and _time(manifest.get('generated_at')) is None:
        errors.append(_issue('INVALID_GENERATED_AT', 'generated_at must be ISO-8601'))
    raw_datasets = manifest.get('datasets')
    if not isinstance(raw_datasets, dict):
        errors.append(_issue('INVALID_DATASETS', 'datasets must be an object'))
        raw_datasets = {}
    names = list(datasets or raw_datasets or active_paths)
    if not configured:
        warnings.append(_issue('MANIFEST_NOT_CONFIGURED', 'using active-file compatibility mode', 'INFO'))
    for name in names:
        try:
            spec = dataset_spec(manifest, name, active_paths.get(name))
        except Exception as exc:
            errors.append(_issue('DATASET_NOT_CONFIGURED', str(exc), dataset=name))
            continue
        dataset_errors, dataset_warnings = [], []
        if spec.get('logical_dataset') != name:
            dataset_errors.append(_issue('LOGICAL_DATASET_MISMATCH', f'{name}: logical_dataset mismatch'))
        if spec.get('ordering') != 'event_time_then_shard_order':
            dataset_errors.append(_issue('UNSUPPORTED_ORDERING', f'{name}: unsupported ordering'))
        shards = spec.get('shards')
        if not isinstance(shards, list):
            dataset_errors.append(_issue('INVALID_SHARDS', f'{name}: shards must be a list'))
            shards = []
        ids, paths, sequences = [], [], []
        intervals = []
        for shard in shards:
            if not isinstance(shard, dict):
                dataset_errors.append(_issue('INVALID_SHARD', f'{name}: shard must be an object'))
                continue
            ids.append(shard.get('shard_id')); paths.append(shard.get('path')); sequences.append(shard.get('sequence'))
            start, end = _time(shard.get('start_time')), _time(shard.get('end_time'))
            if start is None or end is None or start > end:
                dataset_errors.append(_issue('INVALID_SHARD_INTERVAL', f'{name}/{shard.get("shard_id")}: invalid time interval'))
            else:
                intervals.append((start, end, shard.get('shard_id')))
            if shard.get('format') not in FORMATS:
                dataset_errors.append(_issue('INVALID_SHARD_FORMAT', f'{name}/{shard.get("shard_id")}: invalid format'))
            if shard.get('closed') is not True:
                dataset_errors.append(_issue('SHARD_NOT_CLOSED', f'{name}/{shard.get("shard_id")}: archive shards must be closed'))
        for values, code in ((ids, 'DUPLICATE_SHARD_ID'), (paths, 'DUPLICATE_SHARD_PATH'), (sequences, 'DUPLICATE_SHARD_SEQUENCE')):
            if len(values) != len(set(values)):
                dataset_errors.append(_issue(code, f'{name}: duplicate shard metadata'))
        if sequences and sequences != sorted(sequences):
            dataset_errors.append(_issue('UNORDERED_SHARD_SEQUENCE', f'{name}: shard sequence is not increasing'))
        for previous, current in zip(sorted(intervals), sorted(intervals)[1:]):
            if current[0] <= previous[1]:
                dataset_errors.append(_issue('SHARD_TIME_OVERLAP', f'{name}: {previous[2]} overlaps {current[2]}'))
        gaps = spec.get('declared_gaps') or []
        gap_intervals = []
        for gap in gaps:
            start = _time(gap.get('start_time')) if isinstance(gap, dict) else None
            end = _time(gap.get('end_time')) if isinstance(gap, dict) else None
            if not isinstance(gap, dict) or start is None or end is None or start >= end or not gap.get('reason'):
                dataset_errors.append(_issue('INVALID_DECLARED_GAP', f'{name}: invalid declarative gap'))
            else:
                gap_intervals.append((start, end))
        for previous, current in zip(sorted(gap_intervals), sorted(gap_intervals)[1:]):
            if current[0] < previous[1]:
                dataset_errors.append(_issue('DECLARED_GAP_OVERLAP', f'{name}: declared gaps overlap'))
        seen, record_count, previous_time = {}, 0, None
        observed_schemas = set()
        source_details = []
        try:
            _m, _s, sources = source_specs(manifest_path, name, active_paths.get(name))
            for source in sources:
                path = _safe_path(project_dir, source['path'])
                if not os.path.exists(path):
                    if source['source_kind'] == 'active' and not source.get('required', False):
                        source_details.append({'path': source['path'], 'status': 'ABSENT_OPTIONAL_ACTIVE'})
                        continue
                    dataset_errors.append(_issue('MISSING_SOURCE_FILE', f'{name}: missing {source["path"]}'))
                    continue
                if source['source_kind'] == 'shard' and verify_checksums:
                    digest, compressed, uncompressed = _physical_metrics(path, source.get('format'))
                    for field, actual in (('sha256', digest), ('compressed_size', compressed), ('uncompressed_size', uncompressed)):
                        if source.get(field) != actual:
                            dataset_errors.append(_issue('SHARD_METADATA_MISMATCH', f'{name}/{source.get("shard_id")}: {field} mismatch'))
                local_count, local_schemas, local_first, local_last = 0, set(), None, None
                with open_source(path, source.get('format')) as handle:
                    for line_number, line in enumerate(handle, 1):
                        if not line.strip():
                            continue
                        try:
                            record = json.loads(line)
                        except Exception as exc:
                            dataset_errors.append(_issue('INVALID_JSONL', f'{source["path"]}:{line_number}: {exc}'))
                            continue
                        if not isinstance(record, dict):
                            dataset_errors.append(_issue('NON_OBJECT_RECORD', f'{source["path"]}:{line_number}'))
                            continue
                        local_count += 1; record_count += 1
                        stamp = _record_time(record, spec.get('timestamp_fields') or DEFAULT_TIMESTAMP_FIELDS)
                        if stamp is None:
                            dataset_warnings.append(_issue('MISSING_RECORD_TIME', f'{source["path"]}:{line_number}', 'WARNING'))
                        else:
                            local_first = local_first or stamp; local_last = stamp
                            if previous_time and stamp < previous_time:
                                dataset_errors.append(_issue('NON_MONOTONIC_RECORD_TIME', f'{source["path"]}:{line_number}'))
                            previous_time = stamp
                        schemas = [record.get(key) for key in ('schema_version', 'feature_schema_version', 'data_schema_version') if record.get(key) not in (None, '')]
                        local_schemas.update(map(str, schemas)); observed_schemas.update(map(str, schemas))
                        dedupe = _dedupe_value(record, spec.get('deduplication_key'))
                        if dedupe in seen:
                            dataset_errors.append(_issue('DUPLICATE_LOGICAL_RECORD', f'{source["path"]}:{line_number}', first_seen=seen[dedupe]))
                        else:
                            seen[dedupe] = f'{source["path"]}:{line_number}'
                if source['source_kind'] == 'shard':
                    if source.get('record_count') != local_count:
                        dataset_errors.append(_issue('SHARD_RECORD_COUNT_MISMATCH', f'{name}/{source.get("shard_id")}'))
                    declared = sorted(map(str, source.get('schema_versions') or []))
                    if declared != sorted(local_schemas):
                        dataset_errors.append(_issue('SHARD_SCHEMA_MISMATCH', f'{name}/{source.get("shard_id")}'))
                    if local_first and _time(source.get('start_time')) != local_first:
                        dataset_errors.append(_issue('SHARD_START_TIME_MISMATCH', f'{name}/{source.get("shard_id")}'))
                    if local_last and _time(source.get('end_time')) != local_last:
                        dataset_errors.append(_issue('SHARD_END_TIME_MISMATCH', f'{name}/{source.get("shard_id")}'))
                source_details.append({'path': source['path'], 'status': 'OK', 'record_count': local_count})
        except Exception as exc:
            dataset_errors.append(_issue('DATASET_READ_FAILED', f'{name}: {exc}'))
        details[name] = {'valid': not dataset_errors, 'record_count': record_count,
                         'logical_fingerprint': logical_fingerprint(manifest_path, name, active_paths.get(name), project_dir)
                         if not dataset_errors else None,
                         'observed_schema_versions': sorted(observed_schemas), 'sources': source_details,
                         'errors': dataset_errors, 'warnings': dataset_warnings}
        errors.extend(dataset_errors); warnings.extend(dataset_warnings)
    return {'manifest_version': manifest.get('manifest_version'), 'configured': configured,
            'valid': not errors, 'errors': errors, 'warnings': warnings, 'datasets': details}
