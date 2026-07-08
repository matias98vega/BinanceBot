#!/usr/bin/env python3
"""Auditable data repair planner for BinanceBot historical files.

Most plans are dry-run only. The trade-open-backfill plan can write only with
an exact trade_id confirmation and creates backup, checksums and a report.
"""
import argparse
import hashlib
import json
import os
import shutil
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
    'trade_open_backfill': 'Backfill a missing history TRADE_OPEN from an exact trade_analytics OPEN',
    'data_hygiene_backfill': 'Dry-run suggestions for simple missing metadata fields',
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


def _stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def _sha256_file(path):
    digest = hashlib.sha256()
    if not os.path.exists(path):
        return None
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _read_jsonl_lines(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding='utf-8') as f:
        for lineno, line in enumerate(f, 1):
            raw = line.rstrip('\n')
            if not raw.strip():
                rows.append((lineno, raw, None))
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                rows.append((lineno, raw, None))
                continue
            rows.append((lineno, raw, record if isinstance(record, dict) else None))
    return rows


def _json_dumps(record):
    return json.dumps(record, ensure_ascii=False, separators=(',', ':'))


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


def _to_float(value):
    if value in (None, ''):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _get_nested(record, dotted):
    current = record
    for part in dotted.split('.'):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _first_scalar_source(record, fields):
    for field in fields:
        value = record.get(field)
        if value in (None, '') or isinstance(value, (dict, list, tuple, set)):
            continue
        return field, value
    return None, None


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


def _find_exact_analytics_open(project_dir, trade_id):
    path = os.path.join(project_dir, 'trading', 'trade_analytics.jsonl')
    matches = []
    for line, record in _iter_jsonl(path) or []:
        if str(record.get('trade_id') or '') != trade_id:
            continue
        if _classify_trade_record(record) == 'open':
            matches.append({'path': 'trading/trade_analytics.jsonl', 'line': line, 'record': record})
    return matches


def _history_trade_records(project_dir):
    path = os.path.join(project_dir, 'data', 'history', 'trades.jsonl')
    return path, _read_jsonl_lines(path)


def _history_has_exact_open(rows, trade_id):
    for _line, _raw, record in rows:
        if not isinstance(record, dict):
            continue
        if str(record.get('trade_id') or '') == trade_id and _classify_trade_record(record) == 'open':
            return True
    return False


def _history_close_lines(rows, trade_id):
    base_id = _base_trade_id(trade_id)
    close_lines = []
    for line, _raw, record in rows:
        if not isinstance(record, dict):
            continue
        record_id = str(record.get('trade_id') or '')
        if record_id not in {trade_id, f'{base_id}:partial'}:
            continue
        if _classify_trade_record(record) == 'close':
            close_lines.append({'line': line, 'record': record})
    return close_lines


def _historical_version_metadata(source_record, timestamp):
    probe = dict(source_record or {})
    probe.pop('bot_version', None)
    if timestamp not in (None, ''):
        probe['opened_at'] = timestamp
        probe['entry_time'] = timestamp
    inferred = version_history.classify_record(probe)
    inferred_version = inferred.get('version')
    source_version = source_record.get('bot_version') if isinstance(source_record, dict) else None
    if inferred_version and inferred_version != version_history.UNKNOWN_VERSION:
        bot_version = inferred_version
        reason = inferred.get('reason')
    elif source_version:
        bot_version = source_version
        reason = 'source_open_bot_version'
    else:
        bot_version = version_history.UNKNOWN_VERSION
        reason = inferred.get('reason') or 'no_matching_version_range'
    contradicted = bool(
        source_version
        and inferred_version
        and inferred_version != version_history.UNKNOWN_VERSION
        and source_version != inferred_version
    )
    return {
        'bot_version': bot_version,
        'strategy_version': source_record.get('strategy_version') or version_history.STRATEGY_VERSION,
        'data_schema_version': source_record.get('data_schema_version') or version_history.DATA_SCHEMA_VERSION,
        'inferred_bot_version_reason': reason,
        'source_bot_version': source_version,
        'source_bot_version_contradicted_by_timestamp': contradicted,
    }


def _build_history_open_record(source_record, trade_id, source_line=None):
    entry_time = (
        source_record.get('opened_at')
        or source_record.get('entry_time')
        or source_record.get('timestamp')
        or source_record.get('recorded_at')
    )
    version_meta = _historical_version_metadata(source_record, entry_time)
    record = {
        'event_type': 'TRADE_OPEN',
        'recorded_at': source_record.get('recorded_at') or entry_time,
        'trade_id': trade_id,
        'symbol': source_record.get('symbol'),
        'side': str(source_record.get('side') or source_record.get('direction') or '').upper() or None,
        'opened_at': entry_time,
        'closed_at': None,
        'duration_seconds': None,
        'duration_minutes': None,
        'entry_price': _to_float(source_record.get('entry_price')),
        'quantity': _to_float(source_record.get('quantity')),
        'capital_used': _to_float(source_record.get('capital_used') or source_record.get('capital')),
        'wallet': source_record.get('wallet') or 'FUTURES',
        'score': _to_float(source_record.get('score')),
        'atr': _to_float(source_record.get('atr')),
        'atr_pct': _to_float(source_record.get('atr_pct')),
        'rsi': _to_float(source_record.get('rsi')),
        'volatility': _to_float(source_record.get('volatility')),
        'btc_context': source_record.get('btc_context') if isinstance(source_record.get('btc_context'), dict) else {},
        'regime': source_record.get('regime') or source_record.get('market_regime') or 'unknown',
        'market_regime': source_record.get('market_regime'),
        'strategy_version': version_meta['strategy_version'],
        'bot_version': version_meta['bot_version'],
        'exit_price': None,
        'exit_reason': None,
        'pnl_pct': None,
        'pnl_usdt': None,
        'fees': _to_float(source_record.get('fees')),
        'status': 'OPEN',
        'result': None,
        'repair_metadata': {
            'repair_type': 'trade_open_backfill',
            'source_file': 'trading/trade_analytics.jsonl',
            'source_line': source_line,
            'source_trade_id': trade_id,
            'reason': 'missing_trade_open_in_trades_jsonl_but_exact_open_found_in_trade_analytics',
            'inferred_bot_version_reason': version_meta['inferred_bot_version_reason'],
        },
    }
    record['data_schema_version'] = version_meta['data_schema_version']
    if version_meta.get('source_bot_version') not in (None, ''):
        record['repair_metadata']['source_bot_version'] = version_meta.get('source_bot_version')
    if version_meta.get('source_bot_version_contradicted_by_timestamp'):
        record['repair_metadata']['source_bot_version_contradicted_by_timestamp'] = True
    for key in ('version_confidence', 'version_notes'):
        if source_record.get(key) not in (None, ''):
            record[key] = source_record.get(key)
    return record


def build_trade_open_backfill_plan(project_dir=PROJECT_DIR, trade_id='short_WLDUSDT_1782763085'):
    history_path, rows = _history_trade_records(project_dir)
    source_opens = _find_exact_analytics_open(project_dir, trade_id)
    close_lines = _history_close_lines(rows, trade_id)
    has_history_open = _history_has_exact_open(rows, trade_id)
    source = source_opens[0] if source_opens else None
    proposed_record = _build_history_open_record(source['record'], trade_id, source_line=source['line']) if source else None

    if has_history_open:
        classification = 'already_repaired'
        can_apply = False
        recommendation = 'data/history/trades.jsonl already contains an exact TRADE_OPEN for this trade_id.'
    elif len(source_opens) != 1:
        classification = 'source_open_not_unique' if source_opens else 'source_open_missing'
        can_apply = False
        recommendation = 'Expected exactly one matching OPEN in trading/trade_analytics.jsonl before any repair.'
    elif not close_lines:
        classification = 'target_close_missing'
        can_apply = False
        recommendation = 'Expected at least one matching close in data/history/trades.jsonl before backfilling the open.'
    else:
        classification = 'missing_trade_open_in_trades_jsonl_but_exact_open_found_in_trade_analytics'
        can_apply = True
        recommendation = 'Safe to backfill a single TRADE_OPEN from the exact trade_analytics OPEN after backup/checksum.'

    insert_before_line = min((item['line'] for item in close_lines), default=None)
    return {
        'schema_version': REPAIR_SCHEMA_VERSION,
        'generated_at': _now_iso(),
        'mode': 'dry_run',
        'plan': 'trade-open-backfill',
        'project_dir': os.path.abspath(project_dir),
        'trade_id': trade_id,
        'classification': classification,
        'can_apply': can_apply,
        'write_performed': False,
        'target_file': 'data/history/trades.jsonl',
        'source_file': 'trading/trade_analytics.jsonl',
        'source_open_count': len(source_opens),
        'target_close_count': len(close_lines),
        'target_has_open': has_history_open,
        'insert_before_line': insert_before_line,
        'source_open': {
            'path': source['path'],
            'line': source['line'],
            'fields': _public_fields(source['record']),
        } if source else None,
        'target_closes': [
            {'line': item['line'], 'fields': _public_fields(item['record'])}
            for item in close_lines
        ],
        'proposed_record': proposed_record,
        'recommendation': recommendation,
        'notes': [
            'Dry-run only unless --apply and --confirm-trade-id match the trade_id.',
            'The proposed record does not alter PnL, exits or existing close records.',
            'Apply creates backup, before/after checksums and a repair report.',
        ],
    }


def apply_trade_open_backfill(project_dir=PROJECT_DIR, trade_id='short_WLDUSDT_1782763085', confirm_trade_id=None):
    if confirm_trade_id != trade_id:
        return {
            'schema_version': REPAIR_SCHEMA_VERSION,
            'generated_at': _now_iso(),
            'mode': 'apply',
            'plan': 'trade-open-backfill',
            'trade_id': trade_id,
            'write_performed': False,
            'error': 'confirmation_required',
            'message': 'Pass --confirm-trade-id with the exact trade_id to apply this repair.',
        }, 2

    plan = build_trade_open_backfill_plan(project_dir, trade_id)
    if not plan.get('can_apply'):
        result = dict(plan)
        result['mode'] = 'apply'
        result['write_performed'] = False
        result['error'] = 'plan_not_applicable'
        return result, 2

    history_path = os.path.join(project_dir, 'data', 'history', 'trades.jsonl')
    rows = _read_jsonl_lines(history_path)
    before_checksum = _sha256_file(history_path)
    stamp = _stamp()
    backup_dir = os.path.join(project_dir, 'data', 'history', 'backups')
    report_dir = os.path.join(project_dir, 'data', 'history', 'repair_reports')
    os.makedirs(backup_dir, exist_ok=True)
    os.makedirs(report_dir, exist_ok=True)
    backup_path = os.path.join(backup_dir, f'trades.jsonl.{trade_id}.{stamp}.bak')
    shutil.copy2(history_path, backup_path)

    insert_before_line = plan.get('insert_before_line')
    proposed_raw = _json_dumps(plan['proposed_record'])
    output_lines = []
    inserted = False
    for line, raw, _record in rows:
        if not inserted and line == insert_before_line:
            output_lines.append(proposed_raw)
            inserted = True
        output_lines.append(raw)
    if not inserted:
        output_lines.append(proposed_raw)
    with open(history_path, 'w', encoding='utf-8', newline='\n') as f:
        for raw in output_lines:
            f.write(raw.rstrip('\n') + '\n')
    after_checksum = _sha256_file(history_path)

    result = dict(plan)
    result.update({
        'mode': 'apply',
        'write_performed': True,
        'backup_path': os.path.relpath(backup_path, project_dir),
        'before_checksum': before_checksum,
        'after_checksum': after_checksum,
        'inserted_record': plan['proposed_record'],
    })
    report_path = os.path.join(report_dir, f'trade_open_backfill.{trade_id}.{stamp}.json')
    result['report_path'] = os.path.relpath(report_path, project_dir)
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, sort_keys=True, ensure_ascii=False)
    return result, 0


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


def build_data_hygiene_backfill_plan(project_dir=PROJECT_DIR):
    path = os.path.join(project_dir, 'trading', 'trade_analytics.jsonl')
    files_reviewed = 1 if os.path.exists(path) else 0
    records_reviewed = 0
    proposed_changes = []
    optional_unresolved = []

    for line, record in _iter_jsonl(path) or []:
        records_reviewed += 1
        trade_id = record.get('trade_id')
        symbol = record.get('symbol')
        market = record.get('market') if isinstance(record.get('market'), dict) else {}
        capital = record.get('capital') if isinstance(record.get('capital'), dict) else {}

        if _get_nested(record, 'market.regime') in (None, ''):
            source_value = record.get('regime')
            source_field = 'regime'
            if source_value in (None, ''):
                source_value = record.get('market_regime')
                source_field = 'market_regime'
            if source_value not in (None, ''):
                proposed_changes.append({
                    'path': 'trading/trade_analytics.jsonl',
                    'line': line,
                    'trade_id': trade_id,
                    'symbol': symbol,
                    'field': 'market.regime',
                    'source_field': source_field,
                    'value': source_value,
                    'confidence': 'high',
                    'write_allowed': False,
                })

        if capital.get('position_final') in (None, ''):
            source_field, source_value = _first_scalar_source(
                record,
                ('position_final', 'capital_used', 'notional'),
            )
            if source_value not in (None, ''):
                proposed_changes.append({
                    'path': 'trading/trade_analytics.jsonl',
                    'line': line,
                    'trade_id': trade_id,
                    'symbol': symbol,
                    'field': 'capital.position_final',
                    'source_field': source_field,
                    'value': source_value,
                    'confidence': 'medium',
                    'write_allowed': False,
                })
            else:
                optional_unresolved.append({
                    'path': 'trading/trade_analytics.jsonl',
                    'line': line,
                    'trade_id': trade_id,
                    'symbol': symbol,
                    'field': 'capital.position_final',
                    'reason': 'no_reliable_same_record_source',
                })

        if record.get('bot_version') in (None, '', version_history.UNKNOWN_VERSION):
            classified = version_history.classify_record(record)
            version = classified.get('version') or version_history.UNKNOWN_VERSION
            if version != version_history.UNKNOWN_VERSION:
                proposed_changes.append({
                    'path': 'trading/trade_analytics.jsonl',
                    'line': line,
                    'trade_id': trade_id,
                    'symbol': symbol,
                    'field': 'bot_version',
                    'source_field': 'timestamp',
                    'value': version,
                    'confidence': classified.get('confidence') or 'medium',
                    'reason': classified.get('reason'),
                    'write_allowed': False,
                })
            else:
                optional_unresolved.append({
                    'path': 'trading/trade_analytics.jsonl',
                    'line': line,
                    'trade_id': trade_id,
                    'symbol': symbol,
                    'field': 'bot_version',
                    'reason': classified.get('reason') or 'no_matching_version_range',
                })

    return {
        'schema_version': REPAIR_SCHEMA_VERSION,
        'generated_at': _now_iso(),
        'mode': 'dry_run',
        'plan': 'data-hygiene-backfill',
        'project_dir': os.path.abspath(project_dir),
        'files_reviewed': files_reviewed,
        'records_reviewed': records_reviewed,
        'proposed_change_count': len(proposed_changes),
        'unresolved_count': len(optional_unresolved),
        'proposed_changes': proposed_changes,
        'optional_unresolved': optional_unresolved,
        'write_performed': False,
        'notes': [
            'No historical file was modified.',
            'This plan only proposes fields that can be inferred from the same record or version metadata.',
            'Apply mode is intentionally disabled for this plan.',
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description='Build a dry-run data repair plan.')
    parser.add_argument('--project-dir', default=PROJECT_DIR)
    parser.add_argument('--dry-run', action='store_true', default=True)
    parser.add_argument('--plan', choices=('summary', 'version-backfill', 'trade-gap', 'trade-open-backfill', 'data-hygiene-backfill'), default='summary')
    parser.add_argument('--trade-id', default='short_WLDUSDT_1782763085')
    parser.add_argument('--confirm-trade-id', default=None)
    parser.add_argument('--write', action='store_true', help='Reserved for future use; currently rejected.')
    parser.add_argument('--apply', action='store_true', help='Reserved for future use; currently rejected.')
    args = parser.parse_args(argv)

    if (args.write or args.apply) and args.plan != 'trade-open-backfill':
        print('ERROR: write mode is not implemented. This scaffold is dry-run only.', file=sys.stderr)
        return 2

    if (args.write or args.apply) and args.plan == 'trade-open-backfill':
        plan, code = apply_trade_open_backfill(
            args.project_dir,
            trade_id=args.trade_id,
            confirm_trade_id=args.confirm_trade_id,
        )
        print(json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=False))
        return code

    if args.plan == 'version-backfill':
        plan = build_version_backfill_plan(args.project_dir)
    elif args.plan == 'data-hygiene-backfill':
        plan = build_data_hygiene_backfill_plan(args.project_dir)
    elif args.plan == 'trade-gap':
        plan = build_trade_gap_plan(args.project_dir, trade_id=args.trade_id)
    elif args.plan == 'trade-open-backfill':
        plan = build_trade_open_backfill_plan(args.project_dir, trade_id=args.trade_id)
    else:
        plan = build_repair_plan(args.project_dir)
    print(json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
