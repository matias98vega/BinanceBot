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
    'trade_close_without_open': 'Investigate total trade close records without a matching open record',
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
TRADE_INVESTIGATION_FILES = (
    ('jsonl', 'trading/trade_analytics.jsonl'),
    ('jsonl', 'trading/decision_snapshots.jsonl'),
    ('jsonl', 'data/history/trades.jsonl'),
    ('jsonl', 'data/history/decisions.jsonl'),
    ('jsonl', 'data/history/snapshots.jsonl'),
    ('jsonl', 'data/history/features.jsonl'),
    ('jsonl', 'data/history/timeline.jsonl'),
    ('json', 'trading/bot_state.json'),
    ('json', 'data/history/futures_reconciliation_status.json'),
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


def _base_trade_id(trade_id):
    text = str(trade_id or '')
    if ':partial' in text:
        return text.split(':partial', 1)[0]
    return text


def _trade_symbol_from_id(trade_id):
    parts = str(trade_id or '').split('_')
    if len(parts) >= 2:
        return parts[1]
    return None


def _public_fields(record):
    if not isinstance(record, dict):
        return {}
    keys = (
        'event_type', 'trade_id', 'related_trade_id', 'symbol', 'side', 'direction',
        'status', 'timestamp', 'recorded_at', 'opened_at', 'closed_at', 'entry_time',
        'exit_time', 'entry_price', 'exit_price', 'exit_reason', 'pnl_usdt',
        'result', 'source', 'reason', 'event', 'category', 'message',
    )
    return {key: record.get(key) for key in keys if record.get(key) not in (None, '')}


def _record_matches_trade(record, trade_id, base_id, symbol):
    if not isinstance(record, dict):
        return False
    direct_ids = (
        record.get('trade_id'),
        record.get('related_trade_id'),
        record.get('base_trade_id'),
    )
    if any(str(value or '') in {trade_id, base_id} for value in direct_ids):
        return True
    if symbol and str(record.get('symbol') or '').upper() == symbol.upper():
        return True
    details = record.get('details') if isinstance(record.get('details'), dict) else {}
    if any(str(details.get(key) or '') in {trade_id, base_id} for key in ('trade_id', 'related_trade_id', 'base_trade_id')):
        return True
    return False


def _classify_trade_record(record):
    event_type = str(record.get('event_type') or '').upper()
    status = str(record.get('status') or '').upper()
    if event_type == 'TRADE_OPEN' or status == 'OPEN':
        return 'open'
    if event_type == 'TRADE_CLOSE' or status == 'CLOSED':
        return 'close'
    return 'related'


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


def build_trade_gap_plan(project_dir=PROJECT_DIR, trade_id='short_WLDUSDT_1782763085'):
    base_id = _base_trade_id(trade_id)
    symbol = _trade_symbol_from_id(base_id)
    evidence = []
    exact_open_records = []
    exact_close_records = []
    related_open_records = []
    related_records = []
    files_reviewed = 0
    records_reviewed = 0

    for file_type, relpath in TRADE_INVESTIGATION_FILES:
        path = os.path.join(project_dir, *relpath.split('/'))
        if not os.path.exists(path):
            continue
        files_reviewed += 1
        iterator = _iter_jsonl(path) if file_type == 'jsonl' else _iter_json(path)
        for line, record in iterator or []:
            records_reviewed += 1
            if not _record_matches_trade(record, trade_id, base_id, symbol):
                continue
            classification = _classify_trade_record(record)
            item = {
                'path': relpath,
                'line': line,
                'classification': classification,
                'timestamp': _record_timestamp(record),
                'fields': _public_fields(record),
            }
            evidence.append(item)
            record_id = str(record.get('trade_id') or '')
            related_id = str(record.get('related_trade_id') or '')
            if classification == 'open':
                if record_id in {trade_id, base_id}:
                    exact_open_records.append(item)
                else:
                    related_open_records.append(item)
            elif classification == 'close' and record_id == trade_id:
                exact_close_records.append(item)
            elif record_id in {trade_id, base_id} or related_id in {trade_id, base_id}:
                related_records.append(item)

    if exact_open_records:
        classification = 'open_found'
        recommendation = 'No synthetic repair should be planned. Re-run audit and inspect why the open was not matched.'
    elif related_open_records:
        classification = 'related_open_requires_manual_mapping'
        recommendation = 'Manual review can decide whether a related open should be linked to this close in a future audited repair.'
    elif exact_close_records:
        classification = 'requires_manual_review'
        recommendation = 'Do not fabricate an open record automatically. Confirm exchange/timeline evidence before any historical repair.'
    else:
        classification = 'not_found'
        recommendation = 'No local evidence was found. Run this plan on the VPS data that reports the critical error.'

    proposed_actions = [
        {
            'action': 'inspect_evidence',
            'description': 'Review exact close, related symbol records, timeline, decisions and reconciliation status.',
            'write_allowed': False,
        },
        {
            'action': 'backup_before_future_repair',
            'description': 'Any future write must create backups and checksums for affected files first.',
            'write_allowed': False,
        },
    ]
    if classification == 'related_open_requires_manual_mapping':
        proposed_actions.append({
            'action': 'manual_link_to_related_open_candidate',
            'description': 'Potential future repair could add auditable linkage metadata instead of rewriting economics.',
            'write_allowed': False,
        })
    if classification == 'requires_manual_review':
        proposed_actions.append({
            'action': 'manual_reconstruct_open_candidate',
            'description': 'Potential future repair could append a synthetic/imported open marker only after external evidence confirms it.',
            'write_allowed': False,
        })

    return {
        'schema_version': REPAIR_SCHEMA_VERSION,
        'generated_at': _now_iso(),
        'mode': 'dry_run',
        'plan': 'trade-gap',
        'project_dir': os.path.abspath(project_dir),
        'trade_id': trade_id,
        'base_trade_id': base_id,
        'symbol': symbol,
        'files_reviewed': files_reviewed,
        'records_reviewed': records_reviewed,
        'classification': classification,
        'recommendation': recommendation,
        'evidence': evidence[:100],
        'summary': {
            'exact_open_records': len(exact_open_records),
            'exact_close_records': len(exact_close_records),
            'related_open_records': len(related_open_records),
            'related_records': len(related_records),
        },
        'proposed_actions': proposed_actions,
        'write_performed': False,
        'notes': [
            'No historical file was modified.',
            'This plan is diagnostic only and must be reviewed before any repair implementation.',
            'Do not repair total closes without a confirmed open/recovery/import source.',
        ],
    }


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
    parser.add_argument('--plan', choices=('summary', 'version-backfill', 'trade-gap'), default='summary')
    parser.add_argument('--trade-id', default='short_WLDUSDT_1782763085')
    parser.add_argument('--write', action='store_true', help='Reserved for future use; currently rejected.')
    parser.add_argument('--apply', action='store_true', help='Reserved for future use; currently rejected.')
    args = parser.parse_args(argv)

    if args.write or args.apply:
        print('ERROR: write mode is not implemented. This scaffold is dry-run only.', file=sys.stderr)
        return 2

    if args.plan == 'version-backfill':
        plan = build_version_backfill_plan(args.project_dir)
    elif args.plan == 'trade-gap':
        plan = build_trade_gap_plan(args.project_dir, trade_id=args.trade_id)
    else:
        plan = build_repair_plan(args.project_dir)
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
