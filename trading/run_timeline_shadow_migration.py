#!/usr/bin/env python3
"""Build and validate a Timeline shadow layout exclusively under /tmp."""
import argparse
import gzip
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

import archive_manifest
import historical_dataset

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SOURCE = os.path.join(PROJECT_DIR, 'data', 'history', 'timeline.jsonl')
DEFAULT_OUTPUT = '/tmp/binancebot_timeline_shadow'


def _iso_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _write_jsonl(path, records, compressed=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if compressed:
        with open(path, 'wb') as raw:
            with gzip.GzipFile(fileobj=raw, mode='wb', mtime=0) as zipped:
                for record in records:
                    zipped.write(archive_manifest._canonical(record))
    else:
        with open(path, 'wb') as handle:
            for record in records:
                handle.write(archive_manifest._canonical(record))


def _file_metadata(path, relative, records, compressed, sequence, source_commit):
    with open(path, 'rb') as handle:
        raw = handle.read()
    if compressed:
        with gzip.open(path, 'rb') as handle:
            uncompressed = handle.read()
    else:
        uncompressed = raw
    fields = archive_manifest.DEFAULT_TIMESTAMP_FIELDS
    return {
        'shard_id': 'timeline-shadow-001', 'path': relative,
        'format': 'jsonl.gz' if compressed else 'jsonl',
        'start_time': archive_manifest._record_time(records[0], fields).isoformat().replace('+00:00', 'Z'),
        'end_time': archive_manifest._record_time(records[-1], fields).isoformat().replace('+00:00', 'Z'),
        'record_count': len(records), 'uncompressed_size': len(uncompressed),
        'compressed_size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
        'schema_versions': sorted({str(value) for row in records for value in
                                   (row.get('schema_version'), row.get('data_schema_version'))
                                   if value not in (None, '')}),
        'created_at': _iso_now(), 'closed': True, 'source_commit': source_commit, 'sequence': sequence,
    }


def _layout(run_dir, name, shard_records, active_records, compressed, source_commit):
    root = os.path.join(run_dir, name)
    extension = '.jsonl.gz' if compressed else '.jsonl'
    shard_relative = 'archive/timeline-shadow-001' + extension
    active_relative = 'active/timeline.jsonl'
    shard_path = os.path.join(root, shard_relative)
    active_path = os.path.join(root, active_relative)
    _write_jsonl(shard_path, shard_records, compressed)
    _write_jsonl(active_path, active_records, False)
    shard = _file_metadata(shard_path, shard_relative, shard_records, compressed, 1, source_commit)
    manifest = {
        'manifest_version': 1, 'generated_at': _iso_now(),
        'datasets': {'timeline': {
            'logical_dataset': 'timeline', 'active_path': active_relative,
            'ordering': 'event_time_then_shard_order', 'deduplication_key': 'event_id',
            'timestamp_fields': list(archive_manifest.DEFAULT_TIMESTAMP_FIELDS),
            'declared_gaps': [], 'shards': [shard],
        }},
    }
    manifest_path = os.path.join(root, 'archive_manifest.json')
    with open(manifest_path, 'w', encoding='utf-8') as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True); handle.write('\n')
    validation = archive_manifest.validate_manifest(manifest_path, project_dir=root)
    fingerprint = archive_manifest.logical_fingerprint(manifest_path, 'timeline', project_dir=root)
    return {'root': root, 'manifest_path': manifest_path, 'validation': validation,
            'logical_fingerprint': fingerprint, 'shard': shard}


def run_shadow(source=DEFAULT_SOURCE, output=DEFAULT_OUTPUT, split_ratio=0.75,
               observe_seconds=0, poll_interval=1.0):
    output_real = os.path.realpath(output)
    if os.path.commonpath(('/tmp', output_real)) != '/tmp':
        raise ValueError('shadow output must be located under /tmp')
    if not 0 < split_ratio < 1:
        raise ValueError('split_ratio must be between 0 and 1')
    os.makedirs(output_real, exist_ok=True)
    run_dir = tempfile.mkdtemp(prefix='run-', dir=output_real)
    captured = historical_dataset.capture_consistent_prefix(source)
    records = historical_dataset.decode_jsonl(captured['payload'])
    if len(records) < 2:
        raise ValueError('Timeline shadow migration requires at least two records')
    split = min(len(records) - 1, max(1, int(len(records) * split_ratio)))
    shard_records, active_records = records[:split], records[split:]
    monolith_path = os.path.join(run_dir, 'monolith', 'timeline.jsonl')
    _write_jsonl(monolith_path, records, False)
    absent_manifest = os.path.join(run_dir, 'monolith', 'archive_manifest.absent.json')
    monolith_fp = archive_manifest.logical_fingerprint(
        absent_manifest, 'timeline', 'monolith/timeline.jsonl', run_dir)
    source_commit = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=PROJECT_DIR,
                                   text=True, capture_output=True, check=False).stdout.strip() or 'unknown'
    plain = _layout(run_dir, 'plain', shard_records, active_records, False, source_commit)
    compressed = _layout(run_dir, 'gzip', shard_records, active_records, True, source_commit)
    rollback_fp = archive_manifest.logical_fingerprint(
        absent_manifest, 'timeline', 'monolith/timeline.jsonl', run_dir)
    observation = {'status': 'NOT_REQUESTED', 'prefix_preserved': None, 'appended_bytes': 0}
    deadline = time.monotonic() + max(0, observe_seconds)
    while time.monotonic() < deadline:
        time.sleep(min(poll_interval, max(0, deadline - time.monotonic())))
        later = historical_dataset.capture_consistent_prefix(source)
        observation = historical_dataset.compare_prefix(captured, later)
        if observation['status'] != 'UNCHANGED':
            break
    fingerprints = {monolith_fp['sha256'], plain['logical_fingerprint']['sha256'],
                    compressed['logical_fingerprint']['sha256'], rollback_fp['sha256']}
    counts = {monolith_fp['record_count'], plain['logical_fingerprint']['record_count'],
              compressed['logical_fingerprint']['record_count'], rollback_fp['record_count']}
    checks = {
        'monolith_equals_plain': monolith_fp == plain['logical_fingerprint'],
        'monolith_equals_gzip': monolith_fp == compressed['logical_fingerprint'],
        'layout_independent_fingerprint': len(fingerprints) == 1,
        'record_counts_equal': len(counts) == 1,
        'plain_manifest_valid': plain['validation']['valid'],
        'gzip_manifest_valid': compressed['validation']['valid'],
        'rollback_by_ignoring_manifest': rollback_fp == monolith_fp,
        'production_source_unchanged_by_tool': True,
        'production_manifest_not_created': not os.path.exists(os.path.join(PROJECT_DIR, 'data', 'history', 'archive_manifest.json')),
        'production_shards_not_created': not os.path.isdir(os.path.join(PROJECT_DIR, 'data', 'history', 'archive', 'timeline')),
    }
    result = {
        'shadow_schema_version': 1, 'generated_at': _iso_now(), 'source': source,
        'output': run_dir, 'capture': {key:value for key,value in captured.items() if key != 'payload'},
        'split': {'total_records': len(records), 'shard_records': len(shard_records),
                  'active_records': len(active_records)},
        'monolith_fingerprint': monolith_fp, 'plain_layout': plain,
        'gzip_layout': compressed, 'rollback_fingerprint': rollback_fp,
        'concurrent_observation': observation, 'checks': checks,
        'valid': all(checks.values()) and observation.get('prefix_preserved') is not False,
        'limitations': [] if observation['status'] == 'APPEND_OBSERVED' else
                       ['REAL_CONCURRENT_APPEND_NOT_OBSERVED_IN_WINDOW'] if observation['status'] == 'UNCHANGED' else
                       ['CONCURRENT_OBSERVATION_NOT_REQUESTED'] if observation['status'] == 'NOT_REQUESTED' else
                       ['SOURCE_ROTATION_OR_REWRITE_OBSERVED'],
    }
    summary_path = os.path.join(run_dir, 'summary.json')
    with open(summary_path, 'w', encoding='utf-8') as handle:
        json.dump(result, handle, indent=2, sort_keys=True); handle.write('\n')
    return result


def _render(result):
    return '\n'.join([
        'TIMELINE SHADOW MIGRATION', f"Valid: {result['valid']}",
        f"Output: {result['output']}",
        f"Records: {result['split']['total_records']} (shard {result['split']['shard_records']} + active {result['split']['active_records']})",
        f"Fingerprint: {result['monolith_fingerprint']['sha256']}",
        f"Plain manifest: {result['checks']['plain_manifest_valid']}",
        f"Gzip manifest: {result['checks']['gzip_manifest_valid']}",
        f"Rollback ignoring manifest: {result['checks']['rollback_by_ignoring_manifest']}",
        f"Concurrent observation: {result['concurrent_observation']['status']}",
        'Production manifest/shards created: False',
    ])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', default=DEFAULT_SOURCE)
    parser.add_argument('--output', default=DEFAULT_OUTPUT)
    parser.add_argument('--split-ratio', type=float, default=0.75)
    parser.add_argument('--observe-seconds', type=float, default=0)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--strict', action='store_true')
    args = parser.parse_args(argv)
    result = run_shadow(args.source, args.output, args.split_ratio, args.observe_seconds)
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else _render(result))
    return 1 if args.strict and not result['valid'] else 0


if __name__ == '__main__':
    sys.exit(main())
