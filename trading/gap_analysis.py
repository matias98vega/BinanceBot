#!/usr/bin/env python3
"""Reproducible gap classification from persisted evidence only."""
import json
from datetime import datetime, timezone

CLASSIFICATIONS = {
    'EXPLAINED_NO_SIGNAL', 'EXPLAINED_CAPACITY', 'EXPLAINED_RISK_PAUSE',
    'EXPLAINED_MANUAL_PAUSE', 'EXPLAINED_RECONCILIATION', 'EXPLAINED_MAINTENANCE',
    'EXPLAINED_EXCHANGE_DEGRADED', 'PARTIALLY_EXPLAINED', 'UNEXPLAINED_DOWNTIME',
    'UNKNOWN_INSUFFICIENT_EVIDENCE', 'LEGACY_NO_EVIDENCE',
}
DEFAULT_HEARTBEAT_COVERAGE_SECONDS = 1200
DEFAULT_EVIDENCE_MAX_AGE_SECONDS = 1200


def _ts(value):
    if isinstance(value, (int, float)):
        return float(value)
    try: return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except Exception: return None


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def read_jsonl(path):
    rows, corrupt = [], 0
    try:
        with open(path, encoding='utf-8') as handle:
            for line in handle:
                try: row = json.loads(line)
                except Exception: corrupt += 1; continue
                if isinstance(row, dict): rows.append(row)
    except FileNotFoundError: pass
    return rows, corrupt


def evidence_interval(row, heartbeat_coverage=DEFAULT_HEARTBEAT_COVERAGE_SECONDS, inferred_end=None):
    if str(row.get("event_category") or "OPERATIONAL").upper() == "DEBUG": return None
    event = row.get('event_type')
    start = _ts(row.get('started_at') or row.get('observed_at') or row.get('occurred_at'))
    if start is None: return None
    end = _ts(row.get('expected_until') or row.get('completed_at'))
    if event == 'operational_heartbeat': end = start + heartbeat_coverage
    elif event == 'cycle_completed': end = max(start, end or start) + heartbeat_coverage
    elif end is None: end = inferred_end if inferred_end is not None else start
    return {'start': start, 'end': end, 'state': row.get('state'), 'reason_code': row.get('reason_code'),
            'event_type': event, 'source': row.get('source'), 'confidence': row.get('confidence')}


def _classification(intervals, covered, duration, legacy):
    if not intervals: return 'LEGACY_NO_EVIDENCE' if legacy else 'UNEXPLAINED_DOWNTIME'
    ratio = covered / duration if duration else 0
    if ratio < .95: return 'PARTIALLY_EXPLAINED' if ratio > 0 else 'UNKNOWN_INSUFFICIENT_EVIDENCE'
    states = {item.get('state') for item in intervals}
    reasons = {item.get('reason_code') for item in intervals}
    if 'MAINTENANCE' in states: return 'EXPLAINED_MAINTENANCE'
    if 'PAUSED_MANUAL' in states: return 'EXPLAINED_MANUAL_PAUSE'
    if 'PAUSED_RISK' in states: return 'EXPLAINED_RISK_PAUSE'
    if 'BLOCKED_RECONCILIATION' in states: return 'EXPLAINED_RECONCILIATION'
    if states & {'DEGRADED', 'BLOCKED_EXCHANGE_UNKNOWN'}: return 'EXPLAINED_EXCHANGE_DEGRADED'
    if 'BLOCKED_CAPACITY' in states or 'CAPACITY' in reasons: return 'EXPLAINED_CAPACITY'
    return 'EXPLAINED_NO_SIGNAL'


def analyze_gaps(events, evidence, min_gap_hours=6, from_ts=None, to_ts=None,
                 heartbeat_coverage=DEFAULT_HEARTBEAT_COVERAGE_SECONDS):
    points = []
    for row in events:
        stamp = _ts(row.get('recorded_at') or row.get('timestamp') or row.get('opened_at') or row.get('closed_at'))
        if stamp is not None and (from_ts is None or stamp >= from_ts) and (to_ts is None or stamp <= to_ts): points.append((stamp, row))
    points.sort(key=lambda item: item[0])
    ordered_evidence = sorted(evidence, key=lambda row: _ts(row.get('started_at') or row.get('observed_at') or row.get('occurred_at')) or float('inf'))
    raw_intervals = []
    for index, row in enumerate(ordered_evidence):
        inferred_end = None
        if row.get('event_type') == 'operational_state_transition' and not row.get('expected_until') and not row.get('completed_at'):
            for following in ordered_evidence[index + 1:]:
                if following.get('event_type') == 'operational_state_transition':
                    inferred_end = _ts(following.get('started_at') or following.get('observed_at') or following.get('occurred_at'))
                    break
        raw_intervals.append(evidence_interval(row, heartbeat_coverage, inferred_end))
    raw_intervals = [item for item in raw_intervals if item]
    first_evidence = min((item['start'] for item in raw_intervals), default=None)
    gaps = []
    for (start, previous), (end, following) in zip(points, points[1:]):
        duration = end - start
        if duration < float(min_gap_hours) * 3600: continue
        overlaps = []
        segments = []
        for item in raw_intervals:
            left, right = max(start, item['start']), min(end, item['end'])
            if right > left: overlaps.append(item); segments.append((left, right))
        segments.sort(); merged = []
        for left, right in segments:
            if merged and left <= merged[-1][1]: merged[-1] = (merged[-1][0], max(merged[-1][1], right))
            else: merged.append((left, right))
        covered = sum(right - left for left, right in merged)
        uncovered, cursor = [], start
        for left, right in merged:
            if left > cursor: uncovered.append({'start': _iso(cursor), 'end': _iso(left)})
            cursor = max(cursor, right)
        if cursor < end: uncovered.append({'start': _iso(cursor), 'end': _iso(end)})
        classification = _classification(overlaps, covered, duration, first_evidence is None or end < first_evidence)
        gaps.append({
            'start': _iso(start), 'end': _iso(end), 'duration_hours': round(duration / 3600, 2),
            'previous_event': previous.get('trade_id') or previous.get('event_type'),
            'next_event': following.get('trade_id') or following.get('event_type'),
            'evidence_intervals': [{**item, 'start': _iso(item['start']), 'end': _iso(item['end'])} for item in overlaps],
            'uncovered_intervals': uncovered,
            'covered_hours': round(covered / 3600, 2), 'uncovered_hours': round((duration - covered) / 3600, 2),
            'classification': classification, 'confidence': 'HIGH' if covered / duration >= .95 else 'LOW',
            'reasons': sorted({str(item.get('reason_code')) for item in overlaps}),
            'source_files': ['trades.jsonl', 'operational_state.jsonl'],
            'operational_impact': classification in {'UNEXPLAINED_DOWNTIME', 'PARTIALLY_EXPLAINED'},
        })
    return gaps
