#!/usr/bin/env python3
"""Compare local observability files before and after a bot cycle."""
import argparse
import json
import os
from datetime import datetime, timezone
from config_loader import load_config


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_runtime_config = load_config(require_api=False)
STATE_FILE = _runtime_config.state_file
TRADE_ANALYTICS = _runtime_config.analytics_file
DECISION_SNAPSHOTS = _runtime_config.decision_snapshots_file
BASELINE_FILE = _runtime_config.cycle_baseline_file


def _iso_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _file_size(path):
    if not os.path.exists(path):
        return 0
    return os.path.getsize(path)


def _jsonl_stats(path):
    line_count = 0
    corrupt_lines = 0
    records = []
    if not os.path.exists(path):
        return {'line_count': 0, 'corrupt_lines': 0, 'records': records}
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            line_count += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                corrupt_lines += 1
                continue
            if isinstance(record, dict):
                records.append(record)
    return {'line_count': line_count, 'corrupt_lines': corrupt_lines, 'records': records}


def _load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _state_positions_count():
    positions = _load_state().get('positions', [])
    return len(positions) if isinstance(positions, list) else 0


def _state_position_ids():
    positions = _load_state().get('positions', [])
    if not isinstance(positions, list):
        return set()
    return {p.get('id') for p in positions if isinstance(p, dict) and p.get('id')}


def _merged_open_trades(records):
    merged = {}
    for record in records:
        trade_id = record.get('trade_id')
        if not trade_id:
            continue
        merged.setdefault(trade_id, {}).update({k: v for k, v in record.items() if v is not None})
    return [record for record in merged.values() if record.get('status') == 'OPEN']


def _current_metrics():
    trade_stats = _jsonl_stats(TRADE_ANALYTICS)
    snapshot_stats = _jsonl_stats(DECISION_SNAPSHOTS)
    open_trades = _merged_open_trades(trade_stats['records'])
    state_ids = _state_position_ids()
    analytics_ids = {t.get('trade_id') for t in open_trades if t.get('trade_id')}
    last_snapshot = snapshot_stats['records'][-1] if snapshot_stats['records'] else {}
    candidates = last_snapshot.get('candidates') if isinstance(last_snapshot, dict) else []
    if not isinstance(candidates, list):
        candidates = []

    decision_counts = {'accepted': 0, 'rejected': 0, 'skipped': 0}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        decision = candidate.get('decision')
        if decision in decision_counts:
            decision_counts[decision] += 1

    return {
        'timestamp': _iso_now(),
        'trade_lines': trade_stats['line_count'],
        'decision_lines': snapshot_stats['line_count'],
        'trade_size_bytes': _file_size(TRADE_ANALYTICS),
        'decision_size_bytes': _file_size(DECISION_SNAPSHOTS),
        'state_open_positions': _state_positions_count(),
        'analytics_open_trades': len(open_trades),
        'trade_corrupt_lines': trade_stats['corrupt_lines'],
        'decision_corrupt_lines': snapshot_stats['corrupt_lines'],
        'last_snapshot_has_candidates': bool(candidates),
        'last_snapshot_candidate_count': len(candidates),
        'last_snapshot_accepted': decision_counts['accepted'],
        'last_snapshot_rejected': decision_counts['rejected'],
        'last_snapshot_skipped': decision_counts['skipped'],
        'state_missing_in_analytics': sorted(state_ids - analytics_ids),
        'analytics_missing_in_state': sorted(analytics_ids - state_ids),
    }


def _save_baseline(metrics):
    baseline = {
        'timestamp': metrics['timestamp'],
        'trade_analytics_lines': metrics['trade_lines'],
        'decision_snapshots_lines': metrics['decision_lines'],
        'trade_analytics_size_bytes': metrics['trade_size_bytes'],
        'decision_snapshots_size_bytes': metrics['decision_size_bytes'],
        'state_open_positions': metrics['state_open_positions'],
        'analytics_open_trades': metrics['analytics_open_trades'],
        'trade_corrupt_lines': metrics['trade_corrupt_lines'],
        'decision_corrupt_lines': metrics['decision_corrupt_lines'],
    }
    with open(BASELINE_FILE, 'w', encoding='utf-8') as f:
        json.dump(baseline, f, ensure_ascii=False, indent=2)
        f.write('\n')
    return baseline


def _load_baseline():
    if not os.path.exists(BASELINE_FILE):
        return None
    try:
        with open(BASELINE_FILE, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _diff(current, baseline_key, current_key, baseline):
    if not baseline:
        return 'N/A'
    base = baseline.get(baseline_key, 0)
    return current[current_key] - base


def _print_report(metrics, baseline=None):
    aligned = not metrics['state_missing_in_analytics'] and not metrics['analytics_missing_in_state']
    new_corrupt_trade = _diff(metrics, 'trade_corrupt_lines', 'trade_corrupt_lines', baseline)
    new_corrupt_decisions = _diff(metrics, 'decision_corrupt_lines', 'decision_corrupt_lines', baseline)
    if new_corrupt_trade == 'N/A' or new_corrupt_decisions == 'N/A':
        new_corrupt = 'N/A'
    else:
        new_corrupt = new_corrupt_trade + new_corrupt_decisions

    print('POST CYCLE CHECK')
    print(f'- baseline: {"FOUND" if baseline else "MISSING"}')
    if baseline:
        print(f'- baseline timestamp: {baseline.get("timestamp")}')
        print(f'- snapshots before: {baseline.get("decision_snapshots_lines", 0)}')
        print(f'- snapshots after: {metrics["decision_lines"]}')
        print(f'- snapshots delta: {_diff(metrics, "decision_snapshots_lines", "decision_lines", baseline)}')
        print(f'- trade analytics lines delta: {_diff(metrics, "trade_analytics_lines", "trade_lines", baseline)}')
    else:
        print('- snapshots before: N/A')
        print(f'- snapshots after: {metrics["decision_lines"]}')
        print('- snapshots delta: N/A')
        print('- trade analytics lines delta: N/A')
    print(f'- current snapshots: {metrics["decision_lines"]}')
    print(f'- last snapshot has candidates: {metrics["last_snapshot_has_candidates"]}')
    print(f'- last snapshot candidates: {metrics["last_snapshot_candidate_count"]}')
    print(f'- last snapshot accepted: {metrics["last_snapshot_accepted"]}')
    print(f'- last snapshot rejected: {metrics["last_snapshot_rejected"]}')
    print(f'- last snapshot skipped: {metrics["last_snapshot_skipped"]}')
    print(f'- analytics OPEN trades: {metrics["analytics_open_trades"]}')
    print(f'- state open positions: {metrics["state_open_positions"]}')
    print(f'- state analytics aligned: {aligned}')
    print(f'- new corrupt JSONL lines: {new_corrupt}')
    print(f'- decision_snapshots.jsonl size: {metrics["decision_size_bytes"]} bytes')
    print(f'- trade_analytics.jsonl size: {metrics["trade_size_bytes"]} bytes')
    if metrics['state_missing_in_analytics']:
        print(f'- state positions missing in analytics: {len(metrics["state_missing_in_analytics"])}')
    if metrics['analytics_missing_in_state']:
        print(f'- analytics OPEN trades missing in state: {len(metrics["analytics_missing_in_state"])}')


def main():
    parser = argparse.ArgumentParser(description='Post-cycle local observability check')
    parser.add_argument('--save-baseline', action='store_true', help='Save current local counters before a bot cycle')
    args = parser.parse_args()

    metrics = _current_metrics()
    if args.save_baseline:
        baseline = _save_baseline(metrics)
        print('POST CYCLE BASELINE')
        print(f'- baseline file: {BASELINE_FILE}')
        print(f'- timestamp: {baseline["timestamp"]}')
        print(f'- trade_analytics lines: {baseline["trade_analytics_lines"]}')
        print(f'- decision_snapshots lines: {baseline["decision_snapshots_lines"]}')
        print(f'- trade_analytics size: {baseline["trade_analytics_size_bytes"]} bytes')
        print(f'- decision_snapshots size: {baseline["decision_snapshots_size_bytes"]} bytes')
        print(f'- state open positions: {baseline["state_open_positions"]}')
        print(f'- analytics OPEN trades: {baseline["analytics_open_trades"]}')
        return 0

    _print_report(metrics, _load_baseline())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
