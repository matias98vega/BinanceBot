#!/usr/bin/env python3
import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
import gap_analysis
import operational_state

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _date(value):
    if not value: return None
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Analyze operational gaps using persisted evidence only')
    parser.add_argument('--json', action='store_true'); parser.add_argument('--explain', action='store_true')
    parser.add_argument('--from', dest='from_date'); parser.add_argument('--to', dest='to_date')
    parser.add_argument('--min-gap-hours', type=float, default=float(os.getenv('GAP_MIN_DURATION_HOURS', '6')))
    parser.add_argument('--output'); parser.add_argument('--strict', action='store_true')
    parser.add_argument('--trades', default=os.path.join(PROJECT_DIR, 'data', 'history', 'trades.jsonl'))
    parser.add_argument('--evidence', default=operational_state.DEFAULT_FILE)
    args = parser.parse_args(argv)
    trades, corrupt_trades = gap_analysis.read_jsonl(args.trades)
    evidence, corrupt_evidence = gap_analysis.read_jsonl(args.evidence)
    heartbeat_coverage = min(
        int(os.getenv('GAP_HEARTBEAT_COVERAGE_SECONDS', '1200')),
        int(os.getenv('GAP_OPERATIONAL_EVIDENCE_MAX_AGE_SECONDS', '1200')),
    )
    gaps = gap_analysis.analyze_gaps(trades, evidence, args.min_gap_hours, _date(args.from_date), _date(args.to_date), heartbeat_coverage)
    summary = {key: sum(1 for gap in gaps if gap['classification'] == key) for key in sorted(gap_analysis.CLASSIFICATIONS)}
    payload = {'gap_count': len(gaps), 'summary': summary, 'gaps': gaps, 'corrupt_lines': corrupt_trades + corrupt_evidence,
               'evidence_records': len(evidence), 'policy': {'persisted_evidence_only': True, 'systemd_used': False, 'heartbeat_coverage_seconds': heartbeat_coverage}}
    if args.output:
        os.makedirs(args.output, exist_ok=True)
        with open(os.path.join(args.output, 'operational_gaps.json'), 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True); handle.write('\n')
    if args.json: print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print('OPERATIONAL GAP ANALYSIS')
        print(f'Gaps: {len(gaps)} | Evidence records: {len(evidence)} | Corrupt lines: {payload["corrupt_lines"]}')
        for gap in gaps:
            print(f'- {gap["start"]} -> {gap["end"]} ({gap["duration_hours"]}h): {gap["classification"]}')
            if args.explain: print(f'  covered={gap["covered_hours"]}h uncovered={gap["uncovered_hours"]}h reasons={gap["reasons"]}')
    risky = any(gap['classification'] in {'UNEXPLAINED_DOWNTIME', 'PARTIALLY_EXPLAINED'} for gap in gaps)
    return 2 if args.strict and risky else 0


if __name__ == '__main__': raise SystemExit(main())
