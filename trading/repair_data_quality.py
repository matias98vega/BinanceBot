#!/usr/bin/env python3
"""Dry-run only scaffold for future auditable data repairs.

This tool intentionally does not modify historical files yet. It exists to make
future repair work explicit, reviewable and safe.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

import audit_data_quality
import version_history


TRADING_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TRADING_DIR)
REPAIR_SCHEMA_VERSION = 1
REPAIRABLE_ISSUES = {
    'partial_close_base_trade_id': 'Potentially link partial close ids to their base trade id',
    'missing_bot_version': 'Potentially annotate records with inferred bot version metadata',
    'legacy_regime_field': 'Potentially normalize legacy regime aliases into canonical regime',
}
VERSION_BACKFILL_FILES = (
    ('jsonl', 'trading/trade_analytics.jsonl'),
    ('jsonl', 'trading/decision_snapshots.jsonl'),
    ('jsonl', 'data/history/trades.jsonl'),
    ('jsonl', 'data/history/decisions.jsonl'),
    ('jsonl', 'data/history/snapshots.jsonl'),
    ('jsonl', 'data/history/features.jsonl'),
    ('jsonl', 'data/history/timeline.jsonl'),
    ('json', 'trading/bot_state.json'),
    ('json', 'data/history/futures_reconciliation_status.json'),
    ('json', 'data/history/residuals_status.json'),
)


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def build_repair_plan(project_dir=PROJECT_DIR):
    report = audit_data_quality.audit_project(project_dir)
    recommendations = sorted(report.recommendations)
    candidates = []

    for item in report.possible_false_positives:
        message = str(item.get('message') or '').lower()
        trade_id = str(item.get('trade_id') or '')
        if ':partial' in trade_id or 'partial' in message:
            candidates.append({
                'issue_type': 'partial_close_base_trade_id',
                'path': item.get('path'),
                'line': item.get('line'),
                'trade_id': item.get('trade_id'),
                'symbol': item.get('symbol'),
                'proposed_action': 'review_base_trade_relationship',
                'write_allowed': False,
            })

    return {
        'schema_version': REPAIR_SCHEMA_VERSION,
        'generated_at': _now_iso(),
        'mode': 'dry_run',
        'project_dir': os.path.abspath(project_dir),
        'available_repair_types': REPAIRABLE_ISSUES,
        'audit_summary': {
            'files_checked': report.files_checked,
            'records_checked': report.records_checked,
            'critical_errors': len(report.errors),
            'warnings': len(report.warnings),
            'recommendations': recommendations,
        },
        'candidates': candidates,
        'write_performed': False,
        'notes': [
            'No historical file was modified.',
            'Future repairs must create backups, checksums and a detailed report before writing.',
        ],
    }


def _record_timestamp(record):
    if not isinstance(record, dict):
        return None
    return (
        record.get('timestamp')
        or record.get('recorded_at')
        or record.get('opened_at')
        or record.get('closed_at')
        or record.get('entry_time')
        or record.get('exit_time')
        or record.get('updated_at')
    )


def _iter_jsonl(path):
    if not os.path.exists(path):
        return
    with open(path, encoding='utf-8') as f:
        for lineno, line in enumerate(f, 1):
            raw = line.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield lineno, record


def _iter_json(path):
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding='utf-8') as f:
            record = json.load(f)
    except Exception:
        return
    if isinstance(record, dict):
        yield 1, record


def build_version_backfill_plan(project_dir=PROJECT_DIR):
    files_reviewed = 0
    records_reviewed = 0
    missing_version = 0
    classifiable = 0
    unclassifiable = 0
    suggested_versions = {}
    examples = []

    for file_type, relpath in VERSION_BACKFILL_FILES:
        path = os.path.join(project_dir, *relpath.split('/'))
        if not os.path.exists(path):
            continue
        files_reviewed += 1
        iterator = _iter_jsonl(path) if file_type == 'jsonl' else _iter_json(path)
        for line, record in iterator or []:
            records_reviewed += 1
            if record.get('bot_version') not in (None, ''):
                continue
            missing_version += 1
            classified = version_history.classify_record(record)
            version = classified.get('version') or 'unknown'
            if version == 'unknown':
                unclassifiable += 1
            else:
                classifiable += 1
                suggested_versions[version] = suggested_versions.get(version, 0) + 1
            if len(examples) < 25:
                examples.append({
                    'path': relpath,
                    'line': line,
                    'timestamp': _record_timestamp(record),
                    'trade_id': record.get('trade_id'),
                    'symbol': record.get('symbol'),
                    'suggested_version': version,
                    'reason': classified.get('reason'),
                })

    return {
        'schema_version': REPAIR_SCHEMA_VERSION,
        'generated_at': _now_iso(),
        'mode': 'dry_run',
        'plan': 'version-backfill',
        'project_dir': os.path.abspath(project_dir),
        'files_reviewed': files_reviewed,
        'records_reviewed': records_reviewed,
        'records_without_version': missing_version,
        'records_classifiable': classifiable,
        'records_unclassifiable': unclassifiable,
        'suggested_versions': suggested_versions,
        'examples': examples,
        'write_performed': False,
        'notes': [
            'No historical file was modified.',
            'Backfill write mode is intentionally disabled in this iteration.',
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description='Build a dry-run data repair plan.')
    parser.add_argument('--project-dir', default=PROJECT_DIR)
    parser.add_argument('--dry-run', action='store_true', default=True)
    parser.add_argument('--plan', choices=('summary', 'version-backfill'), default='summary')
    parser.add_argument('--write', action='store_true', help='Reserved for future use; currently rejected.')
    parser.add_argument('--apply', action='store_true', help='Reserved for future use; currently rejected.')
    args = parser.parse_args(argv)

    if args.write or args.apply:
        print('ERROR: write mode is not implemented. This scaffold is dry-run only.', file=sys.stderr)
        return 2

    if args.plan == 'version-backfill':
        plan = build_version_backfill_plan(args.project_dir)
    else:
        plan = build_repair_plan(args.project_dir)
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
