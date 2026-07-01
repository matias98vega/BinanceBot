#!/usr/bin/env python3
"""Resumen estadistico de trades cerrados desde trade_analytics.jsonl."""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
from analytics import AnalyticsLogger
import analytics_engine


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _profit_factor(trades):
    gross_profit = sum(_float(t.get('pnl_usdt')) for t in trades if _float(t.get('pnl_usdt')) > 0)
    gross_loss = abs(sum(_float(t.get('pnl_usdt')) for t in trades if _float(t.get('pnl_usdt')) < 0))
    if gross_loss == 0:
        return None if gross_profit == 0 else float('inf')
    return gross_profit / gross_loss


def _win_rate(trades):
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if _float(t.get('pnl_usdt')) > 0)
    return wins / len(trades) * 100


def _avg(values):
    values = list(values)
    if not values:
        return 0.0
    return sum(values) / len(values)


def _fmt(value):
    if value is None:
        return 'N/A'
    if value == float('inf'):
        return 'inf'
    return f'{value:.4f}'


def _print_symbol_rank(title, rows):
    print(title)
    if not rows:
        print('  N/A')
        return
    for symbol, pnl in rows:
        print(f'  {symbol}: {_fmt(pnl)} USDT')


def main():
    trades = AnalyticsLogger().load_closed_trades()
    longs = [t for t in trades if str(t.get('side', '')).upper() == 'LONG']
    shorts = [t for t in trades if str(t.get('side', '')).upper() == 'SHORT']
    wins = [_float(t.get('pnl_usdt')) for t in trades if _float(t.get('pnl_usdt')) > 0]
    losses = [_float(t.get('pnl_usdt')) for t in trades if _float(t.get('pnl_usdt')) < 0]

    by_symbol = defaultdict(float)
    by_regime = defaultdict(list)
    for trade in trades:
        by_symbol[trade.get('symbol') or 'UNKNOWN'] += _float(trade.get('pnl_usdt'))
        by_regime[analytics_engine._trade_regime_value(trade)].append(trade)

    ranked = sorted(by_symbol.items(), key=lambda item: item[1], reverse=True)

    print(f'Trades totales: {len(trades)}')
    print(f'Win Rate: {_fmt(_win_rate(trades))}%')
    print(f'Profit Factor: {_fmt(_profit_factor(trades))}')
    print(f'Ganancia promedio: {_fmt(_avg(wins))} USDT')
    print(f'Perdida promedio: {_fmt(_avg(losses))} USDT')
    print(f'Long Win Rate: {_fmt(_win_rate(longs))}%')
    print(f'Short Win Rate: {_fmt(_win_rate(shorts))}%')
    print(f'Long Profit Factor: {_fmt(_profit_factor(longs))}')
    print(f'Short Profit Factor: {_fmt(_profit_factor(shorts))}')
    print(f'PnL total: {_fmt(sum(_float(t.get("pnl_usdt")) for t in trades))} USDT')
    _print_symbol_rank('Top 5 simbolos:', ranked[:5])
    _print_symbol_rank('Bottom 5 simbolos:', list(reversed(ranked[-5:])))

    print('Resultados por market_regime:')
    if not by_regime:
        print('  N/A')
    for regime, regime_trades in sorted(by_regime.items()):
        pnl = sum(_float(t.get('pnl_usdt')) for t in regime_trades)
        print(
            f'  {regime}: trades={len(regime_trades)} '
            f'win_rate={_fmt(_win_rate(regime_trades))}% '
            f'profit_factor={_fmt(_profit_factor(regime_trades))} '
            f'pnl={_fmt(pnl)} USDT'
        )


if __name__ == '__main__':
    main()
