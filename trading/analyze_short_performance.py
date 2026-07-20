#!/usr/bin/env python3
"""Generate a read-only exploratory SHORT performance report."""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import feature_store
import history
import short_performance
import version_history


def _fmt(value):
    return 'N/A' if value is None else f'{value:.4f}' if isinstance(value, float) else str(value)


def format_text(report):
    universe, summary = report['universe'], report['summary']
    comparisons = sorted(
        report['winner_loser_comparison'].items(),
        key=lambda item: abs(item[1].get('cohen_d') or 0), reverse=True,
    )
    lines = [
        f'SHORT PERFORMANCE: {report["version"]}', 'EXPLORATORY / NOT CAUSAL', '',
        f'Total {universe["total"]} | Closed {universe["closed"]} | Open {universe["open"]} | Wins {universe["winners"]} | Losses {universe["losers"]}',
        f'PnL {_fmt(summary["pnl_total"])} | PF {_fmt(summary["profit_factor"])} | Expectancy {_fmt(summary["expectancy"])} | WR {_fmt(summary["win_rate"])}%',
        f'Bootstrap mean PnL 95% CI: {_fmt((report["bootstrap"]["pnl_mean_95_ci"] or {}).get("lower_95"))} to {_fmt((report["bootstrap"]["pnl_mean_95_ci"] or {}).get("upper_95"))}',
        '', 'REGIMES',
    ]
    for name, bucket in report['regimes'].items():
        lines.append(f'{name}: C={bucket["closed"]} WR={_fmt(bucket["win_rate"])}% PnL={_fmt(bucket["pnl_total"])} PF={_fmt(bucket["profit_factor"])} Exp={_fmt(bucket["expectancy"])}')
    lines.extend(['', 'EXIT REASONS'])
    for name, bucket in report['exit_reasons'].items():
        lines.append(f'{name}: C={bucket["closed"]} PnL={_fmt(bucket["pnl_total"])} Avg={_fmt(bucket["pnl_average"])}')
    lines.extend(['', 'LARGEST WINNER/LOSER FEATURE DIFFERENCES'])
    for name, data in comparisons[:10]:
        lines.append(f'{name}: valid={data["valid"]} diff={_fmt(data["mean_difference"])} d={_fmt(data["cohen_d"])}')
    quality = report['data_quality']
    lines.extend([
        '', 'DATA QUALITY',
        f'Snapshots found={quality["opening_snapshot_found"]} missing={quality["opening_snapshot_missing"]} complete={quality["complete"]} partial={quality["partial"]}',
        '', 'SYMBOLS WORST',
    ])
    for item in report['symbols']['worst']:
        lines.append(f'{item["symbol"]}: C={item["closed"]} PnL={_fmt(item["pnl_total"])} PF={_fmt(item["profit_factor"])} Exp={_fmt(item["expectancy"])}')
    lines.extend(['', 'CANDIDATE RULES (OFFLINE ONLY)'])
    for rule in report['candidate_rules']:
        lines.append(f'{rule["name"]}: retained={rule["retained"]} coverage={_fmt(rule["coverage_percent"])}% PnL {_fmt(rule["original"]["pnl_total"])} -> {_fmt(rule["filtered"]["pnl_total"])} PF {_fmt(rule["original"]["profit_factor"])} -> {_fmt(rule["filtered"]["profit_factor"])}')
    lines.extend(['', f'Flags: {", ".join(report["flags"])}', '', 'LIMITATIONS'])
    lines.extend(f'- {item}' for item in report['limitations'])
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', default=version_history.current_version())
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--min-sample', type=int, default=5)
    parser.add_argument('--top', type=int, default=10)
    parser.add_argument('--trades-file', default=history.DEFAULT_TRADES_FILE, help=argparse.SUPPRESS)
    parser.add_argument('--features-file', default=feature_store.DEFAULT_FEATURES_FILE, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.min_sample < 1 or args.top < 1:
        parser.error('--min-sample and --top must be positive')
    try:
        if not os.path.exists(args.trades_file):
            raise FileNotFoundError(args.trades_file)
        report = short_performance.build_report(args.version, args.min_sample, args.top, args.trades_file, args.features_file)
        print(json.dumps(report, indent=2, sort_keys=True) if args.json else format_text(report))
        return 0
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
