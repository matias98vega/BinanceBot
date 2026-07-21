#!/usr/bin/env python3
"""Local data quality auditor for BinanceBot runtime/history files."""
import argparse
import glob
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import version_history


TRADING_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TRADING_DIR)
MAX_APPEND_GAP_SECONDS = 6 * 60 * 60
GAP_RECENT_WINDOW = timedelta(hours=48)
VALID_SIDES = {'LONG', 'SHORT'}
VALID_STATUS = {'OPEN', 'CLOSED'}
VALID_REGIMES = {'bull', 'bear', 'sideways', 'neutral', 'unknown', 'bullish', 'bearish'}
SENSITIVE_MARKERS = ('key', 'secret', 'token', 'signature', 'header', 'cookie', 'authorization')
CAPITAL_LEDGER_TYPES = {
    'external_deposit',
    'external_withdrawal',
    'rebalance',
    'realized_pnl',
    'commission',
    'funding_fee',
    'initial_capital',
    'manual_adjustment',
    'reconciliation',
    'unknown_capital_flow',
}


class AuditReport:
    def __init__(self):
        self.files_checked = 0
        self.records_checked = 0
        self.errors = []
        self.warnings = []
        self.operational_warnings = []
        self.legacy_warnings = []
        self.accepted_warnings = []
        self.informational_warnings = []
        self.incidents = []
        self.reference_time = None
        self.optional_recommendations = set()
        self.missing_fields = defaultdict(Counter)
        self.complete_records = defaultdict(int)
        self.total_records = defaultdict(int)
        self.recommendations = set()
        self.totals_by_type = defaultdict(float)
        self.critical_examples = []
        self.possible_false_positives = []
        self.incomplete_examples = defaultdict(list)
        self.version_summary = defaultdict(lambda: {'records': 0, 'critical_errors': 0, 'warnings': 0})

    def error(self, path, message):
        self.errors.append(f'{_display_path(path)}: {message}')

    def warning(self, path, message):
        item = f'{_display_path(path)}: {message}'
        self.warnings.append(item)
        self.operational_warnings.append(item)
        self.explain_incident(path, "operational", "existing_operational_rule", "classified_by_existing_audit_rule", None, True)

    def legacy_warning(self, path, message):
        item = f'{_display_path(path)}: {message}'
        self.warnings.append(item)
        self.legacy_warnings.append(item)
        self.explain_incident(path, "legacy", "existing_legacy_rule", "classified_by_existing_audit_rule", None, False)

    def accepted_warning(self, path, message):
        item = f'{_display_path(path)}: {message}'
        self.warnings.append(item)
        self.accepted_warnings.append(item)
        self.explain_incident(path, "accepted", "existing_accepted_rule", "classified_by_existing_audit_rule", None, False)

    def informational_warning(self, path, message):
        item = f"{_display_path(path)}: {message}"
        self.warnings.append(item)
        self.informational_warnings.append(item)
        self.explain_incident(path, "informational", "existing_informational_rule", "classified_by_existing_audit_rule", None, False)

    def info(self, path, message):
        self.informational_warning(path, f"INFO {message}")

    def explain_incident(self, path, category, rule, evidence, timestamp, affects_operational_state):
        display_path = _display_path(path)
        if (not rule.startswith("existing_") and self.incidents and self.incidents[-1]["path"] == display_path
                and self.incidents[-1]["category"] == category
                and self.incidents[-1]["rule"].startswith("existing_")):
            self.incidents.pop()
        self.incidents.append({
            "path": display_path,
            "category": category,
            "rule": rule,
            "evidence": evidence,
            "timestamp": timestamp or "N-D",
            "affects_operational_state": bool(affects_operational_state),
        })

    def record_version(self, record):
        classified = version_history.classify_record(record)
        version = classified.get('version') or 'unknown'
        self.version_summary[version]['records'] += 1
        if version == 'unknown':
            self.recommendations.add('Versioning: registros sin bot_version ni timestamp clasificable requieren backfill auditable opcional.')
        return version

    def record_error(self, path, message, record):
        self.error(path, message)
        version = record.get('_audit_version') if isinstance(record, dict) else None
        self.version_summary[version or 'unknown']['critical_errors'] += 1

    def record_warning(self, path, message, record):
        version = record.get('_audit_version') if isinstance(record, dict) else None
        if isinstance(record, dict) and record.get('_audit_force_operational'):
            self.warning(path, message)
        elif _is_accepted_warning_record(record, message):
            self.accepted_warning(path, message)
        elif _is_legacy_record(record):
            self.legacy_warning(path, message)
        else:
            self.warning(path, message)
        self.version_summary[version or 'unknown']['warnings'] += 1

    def missing(self, path, field):
        self.missing_fields[_display_path(path)][field] += 1

    def completeness(self, path, complete):
        key = _display_path(path)
        self.total_records[key] += 1
        if complete:
            self.complete_records[key] += 1

    def critical_example(self, path, message, record=None):
        if len(self.critical_examples) >= 25:
            return
        self.critical_examples.append(_example_payload(path, message, record))

    def false_positive(self, path, message, record=None):
        if len(self.possible_false_positives) >= 25:
            return
        self.possible_false_positives.append(_example_payload(path, message, record))

    def incomplete_example(self, path, record, missing_fields, recent=False):
        key = _display_path(path)
        bucket = self.incomplete_examples[key]
        if len(bucket) >= 10:
            bucket.pop(0)
        bucket.append({
            'line': record.get('_audit_line'),
            'timestamp': _timestamp_value(record) or 'N/D',
            'missing': list(missing_fields),
            'age': 'recent' if recent else 'historical',
            'trade_id': _get_nested(record, 'identification.trade_id') or record.get('trade_id'),
            'symbol': _get_nested(record, 'identification.symbol') or record.get('symbol'),
        })




def _latest_project_timestamp(project_dir):
    latest = None
    paths = (
        glob.glob(_project_path(project_dir, 'data', 'history', '*.jsonl'))
        + glob.glob(_project_path(project_dir, 'trading', '*.jsonl'))
    )
    for path in paths:
        try:
            with open(path, encoding='utf-8') as file:
                for line in file:
                    try:
                        record = json.loads(line)
                    except Exception:
                        continue
                    dt, _error = _parse_ts(_timestamp_value(record))
                    if dt and (latest is None or dt > latest):
                        latest = dt
        except Exception:
            continue
    for path in (
        _project_path(project_dir, 'trading', 'state.json'),
        _project_path(project_dir, 'trading', 'bot_state.json'),
        _project_path(project_dir, 'data', 'history', 'rebalance_status.json'),
    ):
        try:
            with open(path, encoding='utf-8') as file:
                record = json.load(file)
            candidates = [record]
            if isinstance(record, dict):
                candidates.extend(value for value in record.values() if isinstance(value, dict))
            for candidate in candidates:
                dt, _error = _parse_ts(_timestamp_value(candidate))
                if dt and (latest is None or dt > latest):
                    latest = dt
        except Exception:
            continue
    return latest


def _gap_classification(report, record, gap_end):
    reference = report.reference_time
    recent = bool(reference and gap_end and timedelta(0) <= reference - gap_end <= GAP_RECENT_WINDOW)
    evidence = record.get('_audit_active_runtime_evidence') if isinstance(record, dict) else None
    if not recent:
        return 'legacy', 'gap_historical_outside_recent_window', f'reference={reference.isoformat() if reference else None}; active_runtime_evidence={evidence}', False
    if evidence:
        return 'operational', 'gap_recent_with_active_runtime_evidence', f'reference={reference.isoformat()}; active_runtime_evidence={evidence}', True
    return 'informational', 'gap_recent_runtime_evidence_unknown', f'reference={reference.isoformat()}; active_runtime_evidence={evidence}', False


def _display_path(path):
    try:
        return os.path.relpath(path, PROJECT_DIR)
    except Exception:
        return str(path)


def _project_path(project_dir, *parts):
    return os.path.join(project_dir, *parts)


def _public_record_fields(record):
    if not isinstance(record, dict):
        return {}
    keys = (
        'trade_id', 'symbol', 'side', 'direction', 'status', 'event_type',
        'timestamp', 'recorded_at', 'opened_at', 'closed_at', 'pnl_usdt',
        'entry_price', 'exit_price', 'exit_reason',
    )
    return {key: record.get(key) for key in keys if record.get(key) not in (None, '')}


def _example_payload(path, message, record=None):
    payload = {
        'path': _display_path(path),
        'message': message,
    }
    if isinstance(record, dict):
        trade_id = record.get('trade_id')
        payload.update({
            'line': record.get('_audit_line'),
            'trade_id': trade_id,
            'symbol': record.get('symbol'),
            'side': record.get('side') or record.get('direction'),
            'status': record.get('status'),
            'event_type': record.get('event_type'),
            'timestamp': _timestamp_value(record),
            'pnl_usdt': record.get('pnl_usdt'),
            'is_partial': _is_partial_trade_id(trade_id),
            'fields': _public_record_fields(record),
        })
    return payload


def _parse_ts(value):
    if value in (None, ''):
        return None, 'missing'
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc), None
        except Exception as exc:
            return None, str(exc)
    try:
        text = str(value).replace('Z', '+00:00')
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc), None
    except Exception as exc:
        return None, str(exc)


def _timestamp_value(record):
    for key in ('timestamp', 'recorded_at', 'opened_at', 'closed_at', 'entry_time', 'exit_time', 'last_check', 'last_attempt'):
        value = record.get(key) if isinstance(record, dict) else None
        if value not in (None, ''):
            return value
    return None


def _record_context(record):
    if not isinstance(record, dict):
        return ''
    fields = {
        'line': record.get('_audit_line'),
        'trade_id': record.get('trade_id') or _get_nested(record, 'identification.trade_id'),
        'event_type': record.get('event_type'),
        'status': record.get('status'),
        'bot_version': record.get('_audit_version') or record.get('bot_version'),
    }
    return ' '.join(f'{key}={value}' for key, value in fields.items() if value not in (None, ''))


def _pause_coverage_for_gap(pause_context, previous_dt, current_dt):
    if pause_context is None or previous_dt is None or current_dt is None:
        return None
    covers = getattr(pause_context, 'covers', None)
    if callable(covers):
        return covers(previous_dt, current_dt)
    return None


def _validate_timestamp(report, path, record, previous_dt=None, previous_record=None, require=True, pause_context=None):
    value = _timestamp_value(record)
    dt, err = _parse_ts(value)
    if err == 'missing':
        if require:
            report.warning(path, 'timestamp ausente')
            report.missing(path, 'timestamp')
        return None
    if err:
        report.error(path, f'timestamp invalido: {value}')
        return None
    now = datetime.now(timezone.utc)
    if dt > now:
        report.error(path, f'timestamp futuro: {value}')
    if previous_dt and dt < previous_dt:
        prev_value = _timestamp_value(previous_record) if isinstance(previous_record, dict) else previous_dt.isoformat()
        report.record_warning(path, _timestamp_warning_message('timestamp fuera de orden', prev_value, value, record, previous_record), record)
    source_timestamp = (
        record.get('source_timestamp') or record.get('market_data_timestamp') or record.get('candle_timestamp')
    ) if isinstance(record, dict) else None
    source_timestamp_separated = bool(_is_market_snapshot(record) and source_timestamp and source_timestamp != value)
    if previous_dt and (dt - previous_dt).total_seconds() > MAX_APPEND_GAP_SECONDS and not source_timestamp_separated:
        prev_value = _timestamp_value(previous_record) if isinstance(previous_record, dict) else previous_dt.isoformat()
        gap_hours = round((dt - previous_dt).total_seconds() / 3600, 2)
        message = _timestamp_warning_message(f'gap grande entre registros: {gap_hours}h', prev_value, value, record, previous_record)
        coverage = _pause_coverage_for_gap(pause_context, previous_dt, dt)
        accepted_record = _is_accepted_warning_record(record, message)
        if coverage or accepted_record:
            report.accepted_warning(path, f"{message} justified_by={coverage}" if coverage else f"{message} accepted_by=record_context")
            report.explain_incident(
                path, "accepted", ("gap_explained_by_pause_or_circuit_breaker" if coverage else "gap_accepted_record_context"),
                f"pause_or_circuit_breaker={coverage}" if coverage else "accepted_record_metadata_or_closed_trade", value, False,
            )
        else:
            category, rule, evidence, affects = _gap_classification(report, record, dt)
            classified_message = f"{message} classification={category} rule={rule}"
            if category == "operational":
                report.warning(path, classified_message)
            elif category == "legacy":
                report.legacy_warning(path, classified_message)
            else:
                report.informational_warning(path, classified_message)
            report.explain_incident(path, category, rule, evidence, value, affects)
    return dt


def _timestamp_warning_message(prefix, previous, current, record, previous_record=None):
    if (
        'timestamp fuera de orden' in prefix
        and _is_market_snapshot(record)
        and not _has_backfill_metadata(record)
        and not _is_legacy_record(record)
    ):
        prefix = f'stale_snapshot_timestamp_generated_recently {prefix}'
        record['_audit_force_operational'] = True
    parts = [
        f'{prefix}: previous={previous}',
        f'current={current}',
        _record_context(record),
    ]
    if isinstance(previous_record, dict):
        previous_trade = previous_record.get('trade_id') or _get_nested(previous_record, 'identification.trade_id')
        current_trade = record.get('trade_id') or _get_nested(record, 'identification.trade_id')
        if previous_trade or current_trade:
            parts.append(f'previous_trade_id={previous_trade} current_trade_id={current_trade}')
    source = record.get('source') or record.get('module') or _get_nested(record, 'metadata.source')
    if source:
        parts.append(f'source={source}')
    source_timestamp = record.get('source_timestamp') or record.get('market_data_timestamp') or record.get('candle_timestamp')
    if source_timestamp:
        parts.append(f'source_timestamp={source_timestamp}')
    metadata_flags = _backfill_metadata_flags(record)
    if metadata_flags:
        parts.append(f'metadata={",".join(metadata_flags)}')
    if record.get('_audit_related_trade_closed') is not None:
        parts.append(f'whether_trade_closed_in_history={bool(record.get("_audit_related_trade_closed"))}')
    if record.get('_audit_active_runtime_evidence') is not None:
        parts.append(f'active_runtime_evidence={record.get("_audit_active_runtime_evidence")}')
    return ' '.join(part for part in parts if part)


def _is_number(value, positive=False, allow_zero=True):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    if positive and (number < 0 or (number == 0 and not allow_zero)):
        return False
    return True


def _get_nested(record, dotted):
    current = record
    for part in dotted.split('.'):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _first_present(container, keys):
    if not isinstance(container, dict):
        return None
    for key in keys:
        value = container.get(key)
        if value not in (None, ''):
            return value
    return None


def _is_partial_trade_id(trade_id):
    text = str(trade_id or '')
    return text.endswith(':partial') or ':partial' in text


def _base_trade_id(trade_id):
    text = str(trade_id or '')
    if ':partial' in text:
        return text.split(':partial', 1)[0]
    return text


def _looks_recovered_or_imported(record):
    text = ' '.join(str(record.get(key) or '') for key in ('exit_reason', 'source', 'description', 'event_type')).lower()
    return any(token in text for token in ('recovery', 'recovered', 'import', 'reconcile', 'manual'))


def _is_legacy_record(record):
    if not isinstance(record, dict):
        return False
    version = record.get('_audit_version') or record.get('bot_version')
    if version in {'legacy-pre-history', 'v1.0-alpha'}:
        return True
    if _looks_recovered_or_imported(record):
        return True
    classified = version_history.classify_record(record)
    return classified.get('version') in {'legacy-pre-history', 'v1.0-alpha'}


def _backfill_metadata_flags(record):
    if not isinstance(record, dict):
        return []
    flags = []
    text = ' '.join(str(record.get(key) or '') for key in ('source', 'description', 'reason', 'event_type')).lower()
    metadata = record.get('metadata') if isinstance(record.get('metadata'), dict) else {}
    text = f'{text} ' + ' '.join(str(value or '') for value in metadata.values()).lower()
    for token in ('backfill', 'backfilled', 'imported', 'recovered', 'synthetic'):
        if token in text:
            flags.append(token)
    for key in ('backfilled', 'imported', 'recovered', 'synthetic'):
        if record.get(key) is True or metadata.get(key) is True:
            flags.append(key)
    return sorted(set(flags))


def _has_backfill_metadata(record):
    return bool(_backfill_metadata_flags(record))


def _is_market_snapshot(record):
    return isinstance(record, dict) and str(record.get('event_type') or '').upper() == 'MARKET_SNAPSHOT'


def _record_trade_id(record):
    if not isinstance(record, dict):
        return None
    return record.get('trade_id') or _get_nested(record, 'identification.trade_id')


def _is_accepted_warning_record(record, message=''):
    if not isinstance(record, dict):
        return False
    if _has_backfill_metadata(record):
        return True
    status = str(record.get('status') or '').upper()
    event_type = str(record.get('event_type') or '').upper()
    if ('timestamp fuera de orden' in message or 'gap grande entre registros' in message):
        if status == 'CLOSED' or event_type == 'TRADE_CLOSE':
            return True
        if record.get('_audit_related_trade_closed') is True and not record.get('_audit_active_runtime_evidence'):
            return True
    return False


def _is_recovery_feature_record(record):
    trade_id = _get_nested(record, 'identification.trade_id') or record.get('trade_id')
    context = record.get('decision_context') if isinstance(record.get('decision_context'), dict) else {}
    extra = record.get('extra') if isinstance(record.get('extra'), dict) else {}
    text = ' '.join(str(value or '') for value in (
        trade_id,
        context.get('open_reason'),
        extra.get('source'),
        extra.get('reason'),
    )).lower()
    return 'recovered' in text or 'recovery' in text


def _looks_binance_error(data):
    text = ' '.join(str(data.get(key) or '') for key in (
        'last_error', 'last_message', 'status', 'pending_reason', 'blocked_reason',
    )).lower()
    return any(token in text for token in ('binance', 'http', 'transfer', 'api', 'code=', '-5013', '-2010'))


def _has_sensitive_metadata(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if any(marker in str(key).lower() for marker in SENSITIVE_MARKERS):
                return True
            if _has_sensitive_metadata(item):
                return True
    elif isinstance(value, list):
        return any(_has_sensitive_metadata(item) for item in value)
    return False


class ActiveTradeContext:
    def __init__(self, state=None, bot_state=None, reconciliation=None):
        self.trade_ids = set()
        self.symbol_sides = set()
        self.managed_symbols = set()
        self.aligned = False
        self.reconciled_trade_ids = set()
        if isinstance(state, dict) and isinstance(state.get('spot_position_reconciliations'), dict):
            self.reconciled_trade_ids.update(str(item) for item in state['spot_position_reconciliations'])
        self._collect(state)
        self._collect(bot_state)
        self._collect_reconciliation(reconciliation)

    def _collect(self, value):
        if isinstance(value, dict):
            trade_id = value.get('trade_id') or value.get('id')
            symbol = value.get('symbol')
            side = value.get('side') or value.get('direction')
            status = str(value.get('status') or value.get('state') or '').upper()
            if trade_id and status != 'CLOSED':
                self.trade_ids.add(str(trade_id))
            if symbol:
                if side:
                    self.symbol_sides.add((str(symbol).upper(), str(side).upper()))
                self.managed_symbols.add(str(symbol).upper())
            for item in value.values():
                self._collect(item)
        elif isinstance(value, list):
            for item in value:
                self._collect(item)

    def _collect_reconciliation(self, value):
        if not isinstance(value, dict):
            return
        summary = value.get('summary') if isinstance(value.get('summary'), dict) else {}
        status = str(summary.get('status') or value.get('status') or '').upper()
        self.aligned = bool(summary.get('aligned') or value.get('aligned') or status in {'ALINEADO', 'ALIGNED'})
        self._collect(value)
        for key in ('managed_positions', 'managed_futures_positions', 'positions', 'observed_positions'):
            positions = value.get(key)
            if isinstance(positions, list):
                for position in positions:
                    if isinstance(position, dict) and position.get('managed_in_state') is True:
                        symbol = position.get('symbol')
                        side = position.get('side') or position.get('direction')
                        if symbol:
                            self.managed_symbols.add(str(symbol).upper())
                            if side:
                                self.symbol_sides.add((str(symbol).upper(), str(side).upper()))

    def reconciled_for_open_trade(self, record):
        trade_id = str(record.get('trade_id') or '')
        return trade_id in self.reconciled_trade_ids or _base_trade_id(trade_id) in self.reconciled_trade_ids

    def evidence_for_open_trade(self, record):
        trade_id = str(record.get('trade_id') or '')
        symbol = str(record.get('symbol') or '').upper()
        side = str(record.get('side') or record.get('direction') or '').upper()
        if trade_id and trade_id in self.trade_ids:
            return 'state_trade_id'
        if symbol and side and (symbol, side) in self.symbol_sides:
            return 'managed_symbol_side'
        if symbol and symbol in self.managed_symbols and self.aligned:
            return 'futures_reconciliation_managed_aligned'
        return None


class SafetyPauseContext:
    def __init__(self, windows=None):
        self.windows = list(windows or [])

    def covers(self, previous_dt, current_dt):
        if previous_dt is None or current_dt is None:
            return None
        tolerance = timedelta(minutes=5)
        for window in self.windows:
            start = window.get('start')
            end = window.get('end')
            if not start or not end:
                continue
            if previous_dt >= start - tolerance and current_dt <= end + tolerance:
                reason = window.get('reason') or 'safety_pause'
                return f'safety_pause:{reason}'
        return None


def _parse_audit_time(value):
    if value in (None, ''):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except (TypeError, ValueError, OSError):
            return None
    text = str(value)
    if text.isdigit():
        try:
            return datetime.fromtimestamp(float(text), timezone.utc)
        except (TypeError, ValueError, OSError):
            return None
    dt, err = _parse_ts(text)
    return dt if not err else None


def _pause_window(start, end, reason='safety_pause', source=None):
    start_dt = _parse_audit_time(start)
    end_dt = _parse_audit_time(end)
    if not start_dt or not end_dt or end_dt <= start_dt:
        return None
    return {'start': start_dt, 'end': end_dt, 'reason': reason, 'source': source}


def _collect_pause_windows_from_state(data, source):
    windows = []
    if not isinstance(data, dict):
        return windows
    safety = data.get('safety_pause') if isinstance(data.get('safety_pause'), dict) else {}
    if safety:
        window = _pause_window(
            safety.get('started_at') or safety.get('pause_started_at'),
            safety.get('until') or safety.get('pause_until'),
            safety.get('reason') or 'safety_pause',
            source,
        )
        if window:
            windows.append(window)
    pause_until = data.get('pause_until')
    pause_started_at = data.get('pause_started_at')
    if pause_until and pause_started_at:
        window = _pause_window(
            pause_started_at,
            pause_until,
            data.get('pause_reason') or 'daily_stop_loss_limit',
            source,
        )
        if window:
            windows.append(window)
    return windows


def _collect_pause_windows_from_timeline(path):
    windows = []
    if not os.path.exists(path):
        return windows
    try:
        with open(path, encoding='utf-8') as f:
            for raw in f:
                try:
                    record = json.loads(raw.strip())
                except Exception:
                    continue
                if not isinstance(record, dict):
                    continue
                event = str(record.get('event') or '').lower()
                details = record.get('details') if isinstance(record.get('details'), dict) else {}
                if event not in {'circuit_breaker_pause_started', 'circuit_breaker_paused'}:
                    continue
                start = details.get('pause_started_at') or record.get('timestamp')
                end = details.get('pause_until')
                reason = details.get('reason') or 'daily_stop_loss_limit'
                window = _pause_window(start, end, reason, 'timeline')
                if window:
                    windows.append(window)
    except Exception:
        return windows
    return windows


def _build_safety_pause_context(state, bot_state, timeline_path):
    windows = []
    windows.extend(_collect_pause_windows_from_state(state, 'state'))
    windows.extend(_collect_pause_windows_from_state(bot_state, 'bot_state'))
    windows.extend(_collect_pause_windows_from_timeline(timeline_path))
    return SafetyPauseContext(windows)


def _read_json(path, report, required=False):
    if not os.path.exists(path):
        if required:
            report.warning(path, 'archivo faltante')
        return None
    report.files_checked += 1
    try:
        if os.path.getsize(path) == 0:
            report.warning(path, 'archivo vacio')
            return None
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            report.error(path, 'JSON no es objeto dict')
            return None
        report.records_checked += 1
        data['_audit_version'] = report.record_version(data)
        return data
    except json.JSONDecodeError as exc:
        report.error(path, f'JSON corrupto: {exc}')
        return None
    except Exception as exc:
        report.error(path, f'no se pudo leer JSON: {exc}')
        return None


def _read_jsonl(path, report, required=False, closed_trade_ids=None, active_context=None, pause_context=None):
    records = []
    if not os.path.exists(path):
        if required:
            report.warning(path, 'archivo faltante')
        return records
    report.files_checked += 1
    try:
        if os.path.getsize(path) == 0:
            report.warning(path, 'archivo vacio')
            return records
        previous_dt = None
        previous_record = None
        with open(path, encoding='utf-8') as f:
            for lineno, raw in enumerate(f, 1):
                line = raw.strip()
                if not line:
                    report.warning(path, f'linea vacia #{lineno}')
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    report.error(path, f'linea corrupta #{lineno}: {exc}')
                    continue
                if not isinstance(record, dict):
                    report.error(path, f'linea #{lineno} no es objeto dict')
                    continue
                record['_audit_line'] = lineno
                record['_audit_version'] = report.record_version(record)
                trade_id = _record_trade_id(record)
                if closed_trade_ids is not None and trade_id:
                    record['_audit_related_trade_closed'] = trade_id in closed_trade_ids or _base_trade_id(trade_id) in closed_trade_ids
                if active_context is not None:
                    record['_audit_active_runtime_evidence'] = active_context.evidence_for_open_trade(record) or False
                current_dt = _validate_timestamp(
                    report,
                    path,
                    record,
                    previous_dt=previous_dt,
                    previous_record=previous_record,
                    require=False,
                    pause_context=pause_context,
                )
                if current_dt:
                    previous_dt = current_dt
                    previous_record = record
                records.append(record)
                report.records_checked += 1
    except Exception as exc:
        report.error(path, f'no se pudo leer JSONL: {exc}')
    return records


def _audit_trade_records(path, records, report, active_context=None):
    opens = set()
    closes = set()
    open_status = set()
    open_records = {}
    all_ids = {record.get('trade_id') for record in records if isinstance(record, dict) and record.get('trade_id')}
    base_ids = {_base_trade_id(trade_id) for trade_id in all_ids}
    for record in records:
        trade_id = record.get('trade_id')
        status = str(record.get('status') or '').upper()
        event_type = str(record.get('event_type') or '').upper()
        if trade_id and (event_type == 'TRADE_OPEN' or status == 'OPEN'):
            opens.add(trade_id)
            open_records[trade_id] = record
            base_ids.add(_base_trade_id(trade_id))
    for record in records:
        required = ['trade_id', 'symbol', 'side', 'status']
        complete = True
        for field in required:
            if record.get(field) in (None, ''):
                report.warning(path, f'{field} ausente')
                report.missing(path, field)
                complete = False
        trade_id = record.get('trade_id')
        status = str(record.get('status') or '').upper()
        side = str(record.get('side') or record.get('direction') or '').upper()
        event_type = str(record.get('event_type') or '').upper()
        if side and side not in VALID_SIDES:
            report.error(path, f'side invalido trade_id={trade_id}: {side}')
            report.critical_example(path, f'side invalido trade_id={trade_id}: {side}', record)
            complete = False
        if status and status not in VALID_STATUS:
            report.error(path, f'status invalido trade_id={trade_id}: {status}')
            report.critical_example(path, f'status invalido trade_id={trade_id}: {status}', record)
            complete = False
        if trade_id and (event_type == 'TRADE_OPEN' or status == 'OPEN'):
            if status == 'OPEN':
                open_status.add(trade_id)
        if trade_id and (event_type == 'TRADE_CLOSE' or status == 'CLOSED'):
            closes.add(trade_id)
            if trade_id not in opens and event_type == 'TRADE_CLOSE':
                base_id = _base_trade_id(trade_id)
                if _is_partial_trade_id(trade_id) and base_id in base_ids:
                    message = f'partial_close_with_related_base cierre parcial sin apertura exacta pero base relacionado existe trade_id={trade_id} base={base_id}'
                    report.accepted_warning(path, message)
                    report.false_positive(path, message, record)
                elif _looks_recovered_or_imported(record):
                    message = f'cierre recuperado/importado sin apertura previa trade_id={trade_id}'
                    report.record_warning(path, message, record)
                    report.false_positive(path, message, record)
                else:
                    message = f'cierre total sin apertura previa trade_id={trade_id}'
                    report.record_error(path, message, record)
                    report.critical_example(path, message, record)
                    report.recommendations.add(
                        'Trades: cierres totales sin apertura previa requieren revision manual o migracion auditada con backup.'
                    )
            if record.get('pnl_usdt') is None:
                report.error(path, f'pnl_usdt faltante en CLOSED trade_id={trade_id}')
                report.critical_example(path, f'pnl_usdt faltante en CLOSED trade_id={trade_id}', record)
                report.missing(path, 'pnl_usdt')
                complete = False
            if not _is_number(record.get('exit_price'), positive=True, allow_zero=False):
                report.error(path, f'exit_price invalido en CLOSED trade_id={trade_id}')
                report.critical_example(path, f'exit_price invalido en CLOSED trade_id={trade_id}', record)
                complete = False
        if record.get('entry_price') is not None and not _is_number(record.get('entry_price'), positive=True, allow_zero=False):
            report.error(path, f'entry_price invalido trade_id={trade_id}')
            complete = False
        duration = record.get('duration_seconds')
        if duration is None:
            duration = record.get('duration_minutes')
        if duration is not None and _is_number(duration) and float(duration) < 0:
            report.error(path, f'duration negativa trade_id={trade_id}')
            report.critical_example(path, f'duration negativa trade_id={trade_id}', record)
            complete = False
        report.completeness(path, complete)
    for trade_id in sorted(open_status - closes):
        record = open_records.get(trade_id, {'trade_id': trade_id})
        evidence = active_context.evidence_for_open_trade(record) if active_context else None
        if evidence:
            report.info(path, f'active_open_trade trade_id={trade_id} evidence={evidence}')
        elif active_context and active_context.reconciled_for_open_trade(record):
            report.info(path, f'reconciled_open_history_preserved trade_id={trade_id} source=automatic_spot_position_reconciliation')
        elif str(trade_id).lower() == 't1':
            report.warning(path, f'suspicious_test_record trade abierto sin cierre trade_id={trade_id}')
        else:
            report.warning(path, f'trade abierto sin cierre trade_id={trade_id}')


def _audit_feature_records(path, records, report, trade_ids=None):
    unknown = 0
    total = 0
    trade_ids = trade_ids or set()
    recent_start = max(0, len(records) - 10)
    for idx, record in enumerate(records):
        total += 1
        complete = True
        missing_fields = []
        is_recovery = _is_recovery_feature_record(record)
        required_checks = {
            'trade_id': _get_nested(record, 'identification.trade_id') or record.get('trade_id'),
            'symbol': _get_nested(record, 'identification.symbol') or record.get('symbol'),
            'market.regime': _get_nested(record, 'market.regime'),
            'capital.position_final': _get_nested(record, 'capital.position_final'),
            'symbol_indicators.entry_price': _get_nested(record, 'symbol_indicators.entry_price'),
        }
        if not is_recovery:
            required_checks.update({
                'market.btc_price': _get_nested(record, 'market.btc_price'),
                'market.btc_change_4h': _get_nested(record, 'market.btc_change_4h'),
                'scoring.score_total': _get_nested(record, 'scoring.score_total'),
            })
        else:
            optional_recovery_fields = {
                'market.btc_price': _get_nested(record, 'market.btc_price'),
                'market.btc_change_4h': _get_nested(record, 'market.btc_change_4h'),
                'scoring.score_total': _get_nested(record, 'scoring.score_total'),
            }
            missing_optional = [field for field, value in optional_recovery_fields.items() if value in (None, '')]
            if missing_optional:
                report.record_warning(path, f'recovery feature sin contexto normal de señal trade_id={required_checks.get("trade_id")} missing={missing_optional}', record)
                report.false_positive(
                    path,
                    f'recovery feature usa schema reducido trade_id={required_checks.get("trade_id")}',
                    record,
                )
        for field, value in required_checks.items():
            if value in (None, ''):
                report.record_warning(path, f'{field} faltante', record)
                report.missing(path, field)
                missing_fields.append(field)
                complete = False
        regime = str(required_checks['market.regime'] or 'unknown').lower()
        if regime == 'unknown':
            unknown += 1
        if regime not in VALID_REGIMES:
            report.record_warning(path, f'market.regime no canonico: {regime}', record)
        trade_id = required_checks['trade_id']
        if trade_id and trade_ids and trade_id not in trade_ids:
            report.record_warning(path, f'feature sin trade relacionado trade_id={trade_id}', record)
            missing_fields.append('trade_relation')
        if missing_fields and idx >= recent_start and not is_recovery:
            report.recommendations.add('Feature Store: registro reciente incompleto no-recovery indica posible bug actual de recoleccion.')
        if missing_fields and idx >= recent_start and is_recovery:
            report.recommendations.add('Feature Store: recovery reciente debe conservar capital.position_final y entry_price; no requiere scoring normal.')
        report.completeness(path, complete)
        if not complete or missing_fields:
            report.incomplete_example(path, record, missing_fields, recent=idx >= recent_start)
    if total and unknown / total > 0.5:
        report.warning(path, f'unknown regime excesivo: {unknown}/{total}')
        report.recommendations.add('Revisar persistencia de market.regime en Feature Store.')


def _audit_capital_ledger(path, records, report):
    initials = [r for r in records if str(r.get('type') or '').lower() == 'initial_capital']
    if len(initials) > 1:
        report.error(path, f'INITIAL_CAPITAL duplicado: {len(initials)}')
    for record in records:
        complete = True
        movement_type = str(record.get('type') or '').lower()
        amount = record.get('amount')
        if not movement_type:
            report.error(path, 'type faltante')
            report.missing(path, 'type')
            complete = False
        elif movement_type not in CAPITAL_LEDGER_TYPES:
            report.warning(path, f'type no reconocido: {movement_type}')
        if not _is_number(amount):
            report.error(path, f'amount invalido type={movement_type}')
            report.missing(path, 'amount')
            complete = False
        else:
            amount_f = float(amount)
            report.totals_by_type[movement_type] += amount_f
            if movement_type in {'external_deposit', 'external_withdrawal'} and amount_f < 0:
                report.error(path, f'{movement_type} negativo')
                complete = False
        if not record.get('asset'):
            report.error(path, f'asset faltante type={movement_type}')
            report.missing(path, 'asset')
            complete = False
        if not record.get('timestamp'):
            report.warning(path, f'timestamp faltante type={movement_type}')
            report.missing(path, 'timestamp')
            complete = False
        if movement_type == 'initial_capital':
            metadata = record.get('metadata') if isinstance(record.get('metadata'), dict) else {}
            required = ('baseline_unrealized_pnl', 'baseline_spot_unrealized_pnl', 'baseline_futures_unrealized_pnl', 'open_positions_at_bootstrap', 'accounting_start_timestamp', 'pre_bootstrap_pnl_excluded')
            for field in required:
                if field not in metadata:
                    report.error(path, f'INITIAL_CAPITAL metadata faltante: {field}')
                    complete = False
            spot = metadata.get('spot_real'); futures = metadata.get('futures_real'); equity = metadata.get('observed_equity')
            if all(_is_number(v) for v in (spot, futures, equity)) and abs(float(spot) + float(futures) - float(equity)) > 0.20:
                report.error(path, 'INITIAL_CAPITAL Spot + Futures no coincide con equity')
                complete = False
            positions = metadata.get('open_positions_at_bootstrap')
            baseline = metadata.get('baseline_unrealized_pnl')
            if isinstance(positions, list) and _is_number(baseline):
                values = [p.get('unrealized_pnl') for p in positions if isinstance(p, dict)]
                if not all(_is_number(v) for v in values) or abs(sum(float(v) for v in values) - float(baseline)) > 0.01:
                    report.error(path, 'INITIAL_CAPITAL desglose de posiciones no suma baseline')
                    complete = False
            if metadata.get('pre_bootstrap_pnl_excluded') is not True:
                report.error(path, 'INITIAL_CAPITAL no excluye PnL pre-bootstrap')
                complete = False
        if _has_sensitive_metadata(record.get('metadata')):
            report.error(path, 'metadata contiene datos sensibles')
            complete = False
        report.completeness(path, complete)


def _audit_bot_state(path, data, report):
    if not data:
        return
    market = data.get('market') if isinstance(data.get('market'), dict) else {}
    capital = data.get('capital') if isinstance(data.get('capital'), dict) else {}
    positions = data.get('positions') if isinstance(data.get('positions'), dict) else {}
    field_aliases = {
        'regime': ('regime', 'market_regime', 'btc_regime', 'trend'),
        'btc_price': ('btc_price', 'price', 'btc_last_price'),
        'btc_change_4h': ('btc_change_4h', 'change_4h', 'btc_chg_4h', 'change4h'),
    }
    for canonical, aliases in field_aliases.items():
        if _first_present(market, aliases) in (None, ''):
            report.warning(path, f'market.{canonical} faltante')
            report.missing(path, f'market.{canonical}')
    if 'directional_mode' not in market:
        report.warning(path, 'market.directional_mode faltante')
        report.missing(path, 'market.directional_mode')
    for wallet in ('spot', 'futures'):
        real = capital.get(f'{wallet}_real')
        used = capital.get(f'{wallet}_used')
        if real is not None and used is not None and _is_number(real) and _is_number(used) and float(used) - float(real) > 0.01:
            report.error(path, f'{wallet}_used mayor que {wallet}_real')
    for side in ('long', 'short'):
        state = positions.get(side) if isinstance(positions.get(side), dict) else {}
        current = state.get('current')
        maximum = state.get('max')
        if current is not None and maximum is not None and _is_number(current) and _is_number(maximum) and float(current) > float(maximum):
            report.warning(path, f'posiciones {side} actuales superan max reportado')
        target_maximum = state.get('target_max')
        if current is not None and target_maximum is not None and _is_number(current) and _is_number(target_maximum) and float(current) > float(target_maximum):
            report.warning(path, f'posiciones {side} existentes superan capacidad objetivo; nuevas entradas no incrementables')


def _audit_rebalance(path, data, report):
    if not data or not data.get('pending'):
        return
    for field in ('pending_reason', 'last_check', 'direction', 'amount'):
        if data.get(field) in (None, ''):
            report.error(path, f'pending=true sin {field}')
            report.missing(path, field)
    attempts = data.get('attempts')
    if attempts in (None, 0, '0') and not data.get('blocked_reason'):
        report.error(path, 'pending=true con attempts=0 sin blocked_reason')
        report.missing(path, 'blocked_reason')
    if _looks_binance_error(data):
        missing = [
            field for field in ("last_http_status", "last_binance_code", "last_raw_body")
            if data.get(field) in (None, "")
        ]
        for field in missing:
            report.missing(path, field)
        if missing:
            timestamp = _timestamp_value(data)
            dt, _error = _parse_ts(timestamp)
            reference = report.reference_time
            recent = bool(reference and dt and timedelta(0) <= reference - dt <= GAP_RECENT_WINDOW)
            fields = ",".join(missing)
            evidence = f"pending=true; missing_fields={fields}; reference={reference.isoformat() if reference else None}"
            if recent:
                category = "operational"
                rule = "rebalance_recent_active_error_incomplete"
                report.warning(path, f"error Binance incompleto missing_fields={fields} classification={category} rule={rule}")
            else:
                category = "legacy"
                rule = "rebalance_historical_error_incomplete"
                report.legacy_warning(path, f"error Binance incompleto missing_fields={fields} classification={category} rule={rule}")
            report.explain_incident(path, category, rule, evidence, timestamp, recent)


def _collect_trade_ids(records):
    return {record.get('trade_id') for record in records if isinstance(record, dict) and record.get('trade_id')}


def _collect_closed_trade_ids(records):
    closed = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        trade_id = record.get('trade_id')
        status = str(record.get('status') or '').upper()
        event_type = str(record.get('event_type') or '').upper()
        if trade_id and (status == 'CLOSED' or event_type == 'TRADE_CLOSE'):
            closed.add(str(trade_id))
            closed.add(_base_trade_id(trade_id))
    return closed


def audit_project(project_dir=PROJECT_DIR):
    report = AuditReport()
    report.reference_time = _latest_project_timestamp(project_dir)
    trading_dir = _project_path(project_dir, 'trading')
    history_dir = _project_path(project_dir, 'data', 'history')

    bot_state = _read_json(_project_path(trading_dir, 'bot_state.json'), report, required=True)
    state = _read_json(_project_path(trading_dir, 'state.json'), report)
    futures_reconciliation = _read_json(_project_path(history_dir, 'futures_reconciliation_status.json'), report)
    active_context = ActiveTradeContext(state=state, bot_state=bot_state, reconciliation=futures_reconciliation)
    timeline_path = _project_path(history_dir, 'timeline.jsonl')
    pause_context = _build_safety_pause_context(state, bot_state, timeline_path)

    history_jsonl_paths = sorted(glob.glob(_project_path(history_dir, '*.jsonl')))
    history_records = {}
    trades_path = _project_path(history_dir, 'trades.jsonl')
    trades_history = []
    if trades_path in history_jsonl_paths:
        trades_history = _read_jsonl(trades_path, report, active_context=active_context, pause_context=pause_context)
        history_records[trades_path] = trades_history
    closed_trade_ids = _collect_closed_trade_ids(trades_history)

    trade_analytics = _read_jsonl(
        _project_path(trading_dir, 'trade_analytics.jsonl'),
        report,
        required=True,
        closed_trade_ids=closed_trade_ids,
        active_context=active_context,
        pause_context=pause_context,
    )
    decision_snapshots = _read_jsonl(_project_path(trading_dir, 'decision_snapshots.jsonl'), report, required=True, pause_context=pause_context)
    closed_trade_ids = _collect_closed_trade_ids(trade_analytics + trades_history)
    for path in history_jsonl_paths:
        if path == trades_path:
            continue
        history_records[path] = _read_jsonl(
            path,
            report,
            closed_trade_ids=closed_trade_ids,
            active_context=active_context,
            pause_context=pause_context,
        )

    all_trade_records = trade_analytics + trades_history
    trade_ids = _collect_trade_ids(all_trade_records)
    if trade_analytics:
        _audit_trade_records(_project_path(trading_dir, 'trade_analytics.jsonl'), trade_analytics, report, active_context=active_context)
    if trades_history:
        _audit_trade_records(_project_path(history_dir, 'trades.jsonl'), trades_history, report, active_context=active_context)

    features_path = _project_path(history_dir, 'features.jsonl')
    if features_path in history_records:
        _audit_feature_records(features_path, history_records[features_path], report, trade_ids=trade_ids)

    ledger_path = _project_path(history_dir, 'capital_ledger.jsonl')
    if ledger_path in history_records:
        _audit_capital_ledger(ledger_path, history_records[ledger_path], report)
    else:
        report.informational_warning(ledger_path, 'capital accounting incomplete: ledger no inicializado; PnL Trading y ROI Trading deben mostrarse N/A')
        report.recommendations.add('Capital accounting: ejecutar reconcile_capital_ledger.py --dry-run antes de un bootstrap explícito.')
    _audit_bot_state(_project_path(trading_dir, 'bot_state.json'), bot_state, report)

    for rebalance_path in (
        _project_path(history_dir, 'rebalance_status.json'),
        _project_path(project_dir, 'rebalance_status.json'),
    ):
        _audit_rebalance(rebalance_path, _read_json(rebalance_path, report), report)

    if not decision_snapshots:
        report.recommendations.add('Verificar que decision_snapshots.jsonl se este generando durante los ciclos.')
    if report.totals_by_type:
        report.recommendations.add('Revisar totales del Capital Ledger antes de integrar PnL ajustado.')
    if report.possible_false_positives:
        report.recommendations.add('Posible bug de auditor: revisar clasificacion de cierres parciales/importados antes de reparar datos.')
    if report.incomplete_examples:
        report.recommendations.add('Distinguir datos historicos antiguos de bug de recoleccion antes de migrar o rellenar campos.')
    if any('features.jsonl' in path for path in report.missing_fields):
        report.recommendations.add('Feature Store: si faltantes aparecen en registros recientes, revisar recoleccion; si son antiguos, considerar migracion.')
    if any('bot_state.json' in path for path in report.missing_fields):
        report.recommendations.add('Bot State: si faltan aliases BTC en estado reciente, revisar persistencia de market.btc_price/btc_change_4h.')
    if any('rebalance_status.json' in path for path in report.missing_fields):
        report.recommendations.add('Rebalance: validar si el faltante corresponde a error Binance real o estado bloqueado sin transferencia.')
    if report.errors:
        report.recommendations.add('Corregir errores criticos antes de usar estos datos para decisiones o reporting.')
    return report


def format_report(report, explain=False):
    if report.errors:
        operational_state = "CRITICO"
    elif report.operational_warnings:
        operational_state = "REVISAR"
    else:
        operational_state = "OK"
    lines = [
        'DATA QUALITY AUDIT',
        '',
        f'Archivos revisados: {report.files_checked}',
        f'Registros revisados: {report.records_checked}',
        f'Errores criticos: {len(report.errors)}',
        f'Warnings operativos recientes: {len(report.operational_warnings)}',
        f'Warnings legacy/historicos: {len(report.legacy_warnings)}',
        f"Warnings informativos: {len(report.informational_warnings)}",
        f'Warnings conocidos aceptados: {len(report.accepted_warnings)}',
        f'Warnings totales: {len(report.warnings)}',
        f"Incidentes consolidados: {len(report.incidents)}",
        f"Estado operativo: {operational_state}",
        '',
        'Campos faltantes:',
    ]
    if report.missing_fields:
        for path, counter in sorted(report.missing_fields.items()):
            fields = ', '.join(f'{field}={count}' for field, count in sorted(counter.items()))
            lines.append(f'- {path}: {fields}')
    else:
        lines.append('- ninguno')
    lines.extend(['', 'Completitud:'])
    if report.total_records:
        for path, total in sorted(report.total_records.items()):
            complete = report.complete_records.get(path, 0)
            pct = (complete / total * 100) if total else 0
            lines.append(f'- {path}: {complete}/{total} ({pct:.1f}%)')
    else:
        lines.append('- sin registros auditables')
    if report.totals_by_type:
        lines.extend(['', 'Capital Ledger totales por tipo:'])
        for movement_type, amount in sorted(report.totals_by_type.items()):
            lines.append(f'- {movement_type}: {amount:.8f}')
    lines.extend(['', 'Ejemplos de errores criticos:'])
    if report.critical_examples:
        for item in report.critical_examples:
            details = [
                f'path={item.get("path")}',
                f'line={item.get("line")}',
                f'trade_id={item.get("trade_id")}',
                f'symbol={item.get("symbol")}',
                f'side={item.get("side")}',
                f'status={item.get("status")}',
                f'event_type={item.get("event_type")}',
                f'timestamp={item.get("timestamp")}',
                f'pnl={item.get("pnl_usdt")}',
                f'partial={item.get("is_partial")}',
            ]
            lines.append(f'- {item.get("message")} | ' + ' | '.join(details))
            if item.get('fields'):
                lines.append(f'  campos: {item.get("fields")}')
    else:
        lines.append('- ninguno')
    lines.extend(['', 'Posibles falsos positivos:'])
    if report.possible_false_positives:
        for item in report.possible_false_positives:
            lines.append(
                f'- {item.get("message")} | path={item.get("path")} line={item.get("line")} '
                f'trade_id={item.get("trade_id")} base={_base_trade_id(item.get("trade_id"))}'
            )
    else:
        lines.append('- ninguno')
    if report.incomplete_examples:
        lines.extend(['', 'Ultimos registros incompletos por archivo:'])
        for path, examples in sorted(report.incomplete_examples.items()):
            lines.append(f'- {path}:')
            for example in examples[-10:]:
                lines.append(
                    f'  line={example.get("line")} timestamp={example.get("timestamp")} '
                    f'age={example.get("age")} trade_id={example.get("trade_id")} '
                    f'symbol={example.get("symbol")} missing={example.get("missing")}'
                )
    lines.extend(['', 'Errores criticos:'])
    lines.extend([f'- {item}' for item in report.errors] or ['- ninguno'])
    lines.extend(['', 'Warnings operativos recientes:'])
    lines.extend([f'- {item}' for item in report.operational_warnings[:50]] or ['- ninguno'])
    if len(report.operational_warnings) > 50:
        lines.append(f'- ... {len(report.operational_warnings) - 50} warnings operativos adicionales')
    lines.extend(['', 'Warnings legacy/historicos:'])
    lines.extend([f'- {item}' for item in report.legacy_warnings[:50]] or ['- ninguno'])
    if len(report.legacy_warnings) > 50:
        lines.append(f'- ... {len(report.legacy_warnings) - 50} warnings legacy adicionales')
    lines.extend(["", "Warnings informativos:"])
    lines.extend([f"- {item}" for item in report.informational_warnings[:50]] or ["- ninguno"])
    if len(report.informational_warnings) > 50:
        lines.append(f"- ... {len(report.informational_warnings) - 50} warnings informativos adicionales")
    lines.extend(['', 'Warnings conocidos aceptados:'])
    lines.extend([f'- {item}' for item in report.accepted_warnings[:50]] or ['- ninguno'])
    if len(report.accepted_warnings) > 50:
        lines.append(f'- ... {len(report.accepted_warnings) - 50} warnings aceptados adicionales')
    lines.extend(['', 'DATA QUALITY BY BOT VERSION'])
    if report.version_summary:
        for version, summary in sorted(report.version_summary.items()):
            lines.append(f'{version}:')
            lines.append(f'- records: {summary.get("records", 0)}')
            lines.append(f'- critical_errors: {summary.get("critical_errors", 0)}')
            lines.append(f'- warnings: {summary.get("warnings", 0)}')
            if version == 'unknown':
                lines.append('- recommendation: optional auditable backfill')
    else:
        lines.append('- sin registros con version clasificable')
    lines.extend(['', 'Recomendaciones:'])
    lines.extend([f'- {item}' for item in sorted(report.recommendations)] or ['- ninguna'])
    if explain:
        lines.extend(["", "EXPLAIN WARNING CLASSIFICATION"])
        if report.incidents:
            for incident in report.incidents:
                lines.append(
                    f"- path={incident['path']} category={incident['category']} "
                    f"rule={incident['rule']} timestamp={incident['timestamp']} "
                    f"affects_operational_state={incident['affects_operational_state']} "
                    f"evidence={incident['evidence']}"
                )
        else:
            lines.append("- sin incidentes clasificados")
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Audit BinanceBot collected data quality.')
    parser.add_argument('--project-dir', default=PROJECT_DIR, help='Project root directory')
    parser.add_argument("--explain", action="store_true", help="Explain warning classification rules and evidence")
    args = parser.parse_args(argv)
    report = audit_project(args.project_dir)
    print(format_report(report, explain=args.explain))
    return 1 if report.errors else 0


if __name__ == '__main__':
    sys.exit(main())
