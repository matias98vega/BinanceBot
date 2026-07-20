#!/usr/bin/env python3
"""Read-only accounting layer built on top of the capital ledger."""
import math

import os
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


def classify_observed_capital_change(equity_change, realized_pnl_net_of_fees=0.0, funding_net=0.0, unrealized_pnl_change=None, reference_capital=None, absolute_tolerance=0.20, percentage_tolerance=0.001):
    """Classify only a reconciled residual; never infer deposit/withdrawal without external evidence."""
    values = [_float_or_none(value) for value in (equity_change, realized_pnl_net_of_fees, funding_net, unrealized_pnl_change, reference_capital)]
    if any(value is None for value in values):
        return {"classification": "INCOMPLETE_DATA", "amount": None}
    change, realized, funding, unrealized, reference = values
    residual = _round(change - realized - funding - unrealized)
    tolerance = max(float(absolute_tolerance), abs(reference) * float(percentage_tolerance))
    if abs(residual) <= tolerance:
        return {"classification": "NO_MATERIAL_FLOW", "amount": residual, "tolerance": tolerance}
    return {"classification": "UNKNOWN_CAPITAL_FLOW", "amount": residual, "tolerance": tolerance}


def get_accounting_summary(current_equity=None, starting_equity=0.0,
                           ledger_file=capital_ledger.DEFAULT_LEDGER_FILE, asset=None, unrealized_pnl=0.0, tolerance=0.20):
    summary = {
        'external_deposits': get_external_deposits(ledger_file, asset),
        'external_withdrawals': get_external_withdrawals(ledger_file, asset),
        'net_external_flows': get_net_external_flows(ledger_file, asset),
        'commissions': get_total_commissions(ledger_file, asset),
        'funding': get_total_funding(ledger_file, asset),
        'realized_trading_pnl': get_realized_trading_pnl(ledger_file, asset),
    }
    totals = _totals(ledger_file, asset)
    initial = _round(totals.get(capital_ledger.TYPE_INITIAL_CAPITAL, 0.0))
    realized = summary["realized_trading_pnl"]
    funding = summary["funding"]
    trading_pnl = _round(realized + funding)
    net_flow = summary["net_external_flows"]
    contributed = _round(initial + net_flow)
    unknown = _round(totals.get(capital_ledger.TYPE_UNKNOWN_CAPITAL_FLOW, 0.0))
    adjustment = _round(totals.get(capital_ledger.TYPE_MANUAL_ADJUSTMENT, 0.0))
    ledger_exists = os.path.isfile(ledger_file)
    complete = bool(ledger_exists and initial > 0 and unknown == 0)
    summary.update({
        "initial_capital": initial if ledger_exists else None,
        "net_external_flow": net_flow,
        "net_contributed_capital": contributed if ledger_exists else None,
        "realized_pnl_net_of_fees": realized,
        "trading_fees_informational": summary["commissions"],
        "funding_net": funding,
        "trading_pnl_net": trading_pnl if complete else None,
        "trading_roi_pct": _round(trading_pnl / contributed * 100) if complete and contributed > 0 else None,
        "accounting_complete": complete,
        "accounting_convention": capital_ledger.ACCOUNTING_CONVENTION,
    })
    equity = _float_or_none(current_equity)
    unrealized = _float_or_none(unrealized_pnl)
    if equity is not None and unrealized is not None and ledger_exists:
        expected = _round(initial + net_flow + trading_pnl + unrealized + adjustment)
        difference = _round(equity - expected)
        effective_tolerance = max(float(tolerance or 0), abs(expected) * 0.001)
        status = "ALIGNED" if difference == 0 else ("WITHIN_TOLERANCE" if abs(difference) <= effective_tolerance else "UNEXPLAINED_DIFFERENCE")
        summary.update({"expected_equity": expected, "unexplained_difference": difference, "accounting_status": status, "accounting_tolerance": effective_tolerance})
    else:
        summary.update({"expected_equity": None, "unexplained_difference": None, "accounting_status": "INCOMPLETE_DATA", "accounting_tolerance": None})
    if current_equity is not None:
        summary['adjusted_equity'] = get_adjusted_equity(current_equity, ledger_file, asset)
        summary['adjusted_pnl'] = get_adjusted_pnl(current_equity, starting_equity, ledger_file, asset)
        summary['adjusted_roi'] = get_adjusted_roi(current_equity, starting_equity, ledger_file, asset)
    return summary
