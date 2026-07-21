#!/usr/bin/env python3
"""Read-only capital ledger analysis.

Accounting convention: REALIZED_PNL is net of trading fees; TRADING_FEE is
informational only; signed FUNDING_FEE is added once. Therefore
trading_pnl_net = realized_pnl_net_of_fees + funding_net.
"""
import argparse
import json
import os

import capital_accounting
import capital_ledger
import analytics_engine


def analyze(ledger_file=capital_ledger.DEFAULT_LEDGER_FILE, date_from=None, date_to=None, current_equity=None, observer=None):
    records = capital_ledger.read_history(ledger_file)
    selected = [r for r in records if (not date_from or str(r.get('timestamp') or '')[:10] >= date_from) and (not date_to or str(r.get('timestamp') or '')[:10] <= date_to)]
    ids = [r.get('event_id') for r in selected if r.get('event_id')]
    duplicates = sorted({item for item in ids if ids.count(item) > 1})
    invalid = [i + 1 for i, r in enumerate(selected) if not r.get('type') or capital_ledger._float_or_none(r.get('amount')) is None or not r.get('timestamp')]
    if current_equity is None:
        summary = analytics_engine.get_live_capital_accounting_stats(ledger_file=ledger_file, observer=observer)
    else:
        summary = capital_accounting.get_accounting_summary(current_equity=current_equity, ledger_file=ledger_file)
    return {'ledger': ledger_file, 'exists': os.path.isfile(ledger_file), 'events': len(selected), 'events_by_type': capital_ledger.get_totals_by_type(ledger_file), 'duplicates': duplicates, 'invalid_lines': invalid, 'metrics': summary, 'gaps': ['bootstrap_required'] if not summary.get('accounting_complete') else [], 'convention_explain': 'REALIZED_PNL is net of trading fees; TRADING_FEE is informational and is not subtracted; signed FUNDING_FEE is added. trading_pnl_net = realized_pnl_net_of_fees + funding_net.'}


def format_text(result, explain=False):
    m = result['metrics']
    lines = ['CAPITAL ACCOUNTING ANALYSIS', f"Ledger: {result['ledger']}", f"Exists: {result['exists']}", f"Events: {result['events']}", f"Accounting status: {m.get('accounting_status')}", f"Accounting complete: {m.get('accounting_complete')}", f"Observation complete: {m.get('observation_complete')}", f"Observation timestamp: {m.get('observation_timestamp')}", f"Missing fields: {m.get('missing_fields') or []}", f"Accounting starts: {m.get('accounting_start_timestamp')}", f"Initial capital: {m.get('initial_capital')}", f"Baseline unrealized PnL: {m.get('baseline_unrealized_pnl')}", f"Current Spot unrealized PnL: {m.get('current_spot_unrealized_pnl')}", f"Current Futures unrealized PnL: {m.get('current_futures_unrealized_pnl')}", f"Current unrealized PnL: {m.get('current_unrealized_pnl')}", f"Unrealized change since bootstrap: {m.get('unrealized_change_since_bootstrap')}", f"External deposits: {m.get('external_deposits')}", f"External withdrawals: {m.get('external_withdrawals')}", f"Net external flow: {m.get('net_external_flow')}", f"Realized PnL net of fees: {m.get('realized_pnl_net_of_fees')}", f"Trading fees (informational): {m.get('trading_fees_informational')}", f"Funding net: {m.get('funding_net')}", f"Trading PnL net: {m.get('trading_pnl_net')}", f"Trading ROI: {m.get('trading_roi_pct')}", f"Unexplained difference: {m.get('unexplained_difference')}", f"Position breakdown: {m.get('current_unrealized_pnl_by_position') or []}", f"Duplicates: {len(result['duplicates'])}", f"Invalid events: {len(result['invalid_lines'])}"]
    if explain:
        lines.extend(['', 'Convention:', result['convention_explain'], 'Formula: expected_equity = initial_capital + net_external_flow + realized_pnl_net_of_fees_since_bootstrap + funding_net_since_bootstrap + current_unrealized_pnl - baseline_unrealized_pnl + recognized_adjustments.', 'A new gross-PnL source requires a new schema/accounting convention, explicit migration, and compatibility tests.'])
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--from', dest='date_from')
    parser.add_argument('--to', dest='date_to')
    parser.add_argument('--explain', action='store_true')
    parser.add_argument('--ledger', default=capital_ledger.DEFAULT_LEDGER_FILE)
    args = parser.parse_args(argv)
    result = analyze(args.ledger, args.date_from, args.date_to)
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else format_text(result, args.explain))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
