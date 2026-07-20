#!/usr/bin/env python3
"""Read-only reproducible bot-version performance report."""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import analytics_engine
import history
import version_history


def _value(value, digits=4):
    if value is None:
        return 'N/A'
    if isinstance(value, float):
        return f'{value:.{digits}f}'
    return str(value)


def format_text(report):
    summary = report['summary']
    lines = [
        f'BOT VERSION PERFORMANCE: {report["version"]}',
        '',
        'SUMMARY',
        f'Trades: {summary["trades"]} | Open: {summary["open"]} | Closed: {summary["closed"]}',
        f'Wins: {summary["win"]} | Losses: {summary["loss"]} | Win rate: {_value(summary["win_rate"])}%',
        f'Closed PnL: {_value(summary["pnl_total"])} | Profit factor: {_value(summary["profit_factor"])} | Expectancy: {_value(summary["expectancy"])}',
        f'First trade: {_value(summary.get("first_trade"))} | Last trade: {_value(summary.get("last_trade"))}',
        f'Flags: {", ".join(report["flags"]) or "none"}',
        '',
        'LONG VS SHORT',
    ]
    for name, bucket in report['by_side'].items():
        lines.append(f'{name}: trades={bucket["trades"]} closed={bucket["closed"]} wr={_value(bucket["win_rate"])} pnl={_value(bucket["pnl_total"])} pf={_value(bucket["profit_factor"])} exp={_value(bucket["expectancy"])}')
    lines.append('')
    lines.append('REGIMES')
    for name, bucket in report['by_regime'].items():
        lines.append(f'{name}: trades={bucket["trades"]} closed={bucket["closed"]} wr={_value(bucket["win_rate"])} pnl={_value(bucket["pnl_total"])} pf={_value(bucket["profit_factor"])} exp={_value(bucket["expectancy"])}')
    lines.append('')
    lines.append('EXIT REASONS')
    for name, item in report['by_exit_reason'].items():
        lines.append(f'{name}: closed={item["closed"]} pnl={_value(item["pnl_total"])} avg={_value(item["pnl_average"])} share={_value(item["closed_percent"])}%')
    concentration = report['concentration']
    lines.extend([
        '',
        'LOSS CONCENTRATION',
        f'Total negative PnL: {_value(concentration["total_negative_pnl"])}',
        f'Top 3 symbols: {_value(concentration["top3_symbol_loss_percent"])}%',
        f'Top 5 symbols: {_value(concentration["top5_symbol_loss_percent"])}%',
        f'Side: {_value(concentration["largest_loss_side"])} {_value(concentration["largest_loss_side_percent"])}%',
        f'Regime: {_value(concentration["largest_loss_regime"])} {_value(concentration["largest_loss_regime_percent"])}%',
        f'SL + preventive: {_value(concentration["sl_preventive_loss_percent"])}%',
        '',
        'SYMBOLS (worst to best)',
    ])
    for item in report['symbol_ranking']:
        lines.append(f'{item["symbol"]}: closed={item["closed"]} wins={item["win"]} losses={item["loss"]} wr={_value(item["win_rate"])} pnl={_value(item["pnl_total"])} pf={_value(item["profit_factor"])} exp={_value(item["expectancy"])}')
    sizing = report['sizing']
    lines.extend([
        '',
        'SIZE AND EXPOSURE',
        f'Sample: {sizing["sample_size"]} | Average: {_value(sizing["average"])}',
        f'Winner average: {_value(sizing["winner_average"])} | Loser average: {_value(sizing["loser_average"])}',
        f'PnL per unit: {_value(sizing["pnl_per_unit"], 8)}',
        f'Tercile bounds: {json.dumps(sizing["tercile_bounds"], sort_keys=True)}',
        f'Distribution: {json.dumps(sizing["distribution"], sort_keys=True)}',
        '',
        'NORMALIZATION RULES',
    ])
    for name, rule in report['normalization_rules'].items():
        lines.append(f'{name}: {rule}')
    lines.append('')
    lines.append('FLAG RULES')
    for name, rule in report['flag_rules'].items():
        lines.append(f'{name}: {rule}')
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', default=version_history.current_version())
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--trades-file', default=history.DEFAULT_TRADES_FILE, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if not os.path.exists(args.trades_file):
            raise FileNotFoundError(args.trades_file)
        report = analytics_engine.analyze_version_performance(args.version, args.trades_file)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(format_text(report))
        return 0
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
