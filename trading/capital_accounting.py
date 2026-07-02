#!/usr/bin/env python3
"""Read-only accounting layer built on top of the capital ledger."""
import math

import capital_ledger


def _float_or_none(value):
    try:
        if value is None or value == '':
            return None
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return None
        return result
    except (TypeError, ValueError):
        return None


def _round(value):
    return round(float(value or 0.0), 8)


def _totals(ledger_file=capital_ledger.DEFAULT_LEDGER_FILE, asset=None):
    return capital_ledger.get_totals_by_type(ledger_file=ledger_file, asset=asset)


def get_external_deposits(ledger_file=capital_ledger.DEFAULT_LEDGER_FILE, asset=None):
    return _round(_totals(ledger_file, asset).get(capital_ledger.TYPE_EXTERNAL_DEPOSIT, 0.0))


def get_external_withdrawals(ledger_file=capital_ledger.DEFAULT_LEDGER_FILE, asset=None):
    return _round(_totals(ledger_file, asset).get(capital_ledger.TYPE_EXTERNAL_WITHDRAWAL, 0.0))


def get_net_external_flows(ledger_file=capital_ledger.DEFAULT_LEDGER_FILE, asset=None):
    return _round(get_external_deposits(ledger_file, asset) - get_external_withdrawals(ledger_file, asset))


def get_total_commissions(ledger_file=capital_ledger.DEFAULT_LEDGER_FILE, asset=None):
    return _round(_totals(ledger_file, asset).get(capital_ledger.TYPE_COMMISSION, 0.0))


def get_total_funding(ledger_file=capital_ledger.DEFAULT_LEDGER_FILE, asset=None):
    return _round(_totals(ledger_file, asset).get(capital_ledger.TYPE_FUNDING_FEE, 0.0))


def get_realized_trading_pnl(ledger_file=capital_ledger.DEFAULT_LEDGER_FILE, asset=None):
    return _round(_totals(ledger_file, asset).get(capital_ledger.TYPE_REALIZED_PNL, 0.0))


def get_adjusted_equity(current_equity, ledger_file=capital_ledger.DEFAULT_LEDGER_FILE, asset=None):
    """Return equity net of external flows.

    Assumption: current_equity is the current account equity in the selected asset.
    External deposits are removed and external withdrawals are added back. Internal
    flows such as rebalance are not adjusted because they do not change total equity.
    """
    equity = _float_or_none(current_equity)
    if equity is None:
        return None
    return _round(equity - get_external_deposits(ledger_file, asset) + get_external_withdrawals(ledger_file, asset))


def get_adjusted_pnl(current_equity, starting_equity=0.0, ledger_file=capital_ledger.DEFAULT_LEDGER_FILE, asset=None):
    """Return preliminary PnL adjusted for external flows.

    Assumption: starting_equity is the baseline equity before the measured period.
    When no baseline is provided, the result is equivalent to adjusted equity.
    """
    adjusted_equity = get_adjusted_equity(current_equity, ledger_file=ledger_file, asset=asset)
    baseline = _float_or_none(starting_equity)
    if adjusted_equity is None or baseline is None:
        return None
    return _round(adjusted_equity - baseline)


def get_adjusted_roi(current_equity, starting_equity, ledger_file=capital_ledger.DEFAULT_LEDGER_FILE, asset=None):
    """Return preliminary adjusted ROI percentage.

    Assumption: starting_equity is positive and represents capital at risk before
    external flows. Returns None when the denominator is missing or zero.
    """
    baseline = _float_or_none(starting_equity)
    pnl = get_adjusted_pnl(current_equity, starting_equity, ledger_file=ledger_file, asset=asset)
    if pnl is None or not baseline:
        return None
    return _round(pnl / baseline * 100)


def get_accounting_summary(current_equity=None, starting_equity=0.0,
                           ledger_file=capital_ledger.DEFAULT_LEDGER_FILE, asset=None):
    summary = {
        'external_deposits': get_external_deposits(ledger_file, asset),
        'external_withdrawals': get_external_withdrawals(ledger_file, asset),
        'net_external_flows': get_net_external_flows(ledger_file, asset),
        'commissions': get_total_commissions(ledger_file, asset),
        'funding': get_total_funding(ledger_file, asset),
        'realized_trading_pnl': get_realized_trading_pnl(ledger_file, asset),
    }
    if current_equity is not None:
        summary['adjusted_equity'] = get_adjusted_equity(current_equity, ledger_file, asset)
        summary['adjusted_pnl'] = get_adjusted_pnl(current_equity, starting_equity, ledger_file, asset)
        summary['adjusted_roi'] = get_adjusted_roi(current_equity, starting_equity, ledger_file, asset)
    return summary
