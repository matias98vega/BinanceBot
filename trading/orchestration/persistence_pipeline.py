#!/usr/bin/env python3
"""Persistence and passive observability helpers for the main bot cycle."""

import bot_state
import config
import market


def safe_log_open(pos, candidate, btc_ctx, capital_at_entry, analytics):
    try:
        observed_capital = capital_at_entry
        if observed_capital is None:
            try:
                observed_capital = float(pos.get('entry_price') or 0) * float(pos.get('quantity') or 0)
            except (TypeError, ValueError):
                observed_capital = None
        analytics.log_trade_open(
            trade_id=pos.get('id'),
            symbol=pos.get('symbol'),
            side=pos.get('direction', '').upper(),
            entry_time=pos.get('entry_time'),
            entry_price=pos.get('entry_price'),
            market_regime=btc_ctx.get('trend') if btc_ctx else None,
            score=candidate.get('score') if candidate else None,
            rsi=candidate.get('rsi') if candidate else None,
            atr=candidate.get('atr') if candidate else pos.get('atr'),
            ema20=candidate.get('ema20') if candidate else None,
            ema50=candidate.get('ema50') if candidate else None,
            volume_ratio=candidate.get('volume_ratio') if candidate else None,
            macd_hist=candidate.get('macd_hist') if candidate else None,
            atr_pct=candidate.get('atr_pct') if candidate else None,
            btc_correlation=(candidate.get('btc_correlation') if candidate and 'btc_correlation' in candidate else
                             candidate.get('corr_btc') if candidate else None),
            reject_reason=candidate.get('reject_reason') if candidate else None,
            reject_reasons=candidate.get('reject_reasons') if candidate else None,
            capital_at_entry=observed_capital,
            quantity=pos.get('quantity'),
            wallet='SPOT' if pos.get('direction') == 'long' else 'FUTURES',
            btc_context=btc_ctx or {},
        )
    except Exception:
        pass


def safe_log_close(pos, exit_price, exit_reason, pnl, analytics):
    try:
        analytics.log_trade_close(
            trade_id=pos.get('id'),
            symbol=pos.get('symbol'),
            side=pos.get('direction', '').upper(),
            entry_time=pos.get('entry_time'),
            entry_price=pos.get('entry_price'),
            exit_price=exit_price,
            exit_reason=exit_reason,
            pnl_usdt=pnl,
        )
    except Exception:
        pass


def safe_log_decision_snapshot(
    btc_ctx,
    spot_total_capital,
    spot_balance,
    futures_balance,
    decisions_logger,
    binance,
):
    try:
        decisions = market.get_last_decision_candidates()
        candidates = decisions.get('long', []) + decisions.get('short', [])
        futures_total = binance.get_total_futures()
        mode = btc_ctx.get('force_mode') or ('directional' if config.DIRECTIONAL_MODE else 'both_sides')
        decisions_logger.log_snapshot(
            market_regime=btc_ctx.get('trend'),
            btc_change_1h=btc_ctx.get('change_1h'),
            btc_change_4h=btc_ctx.get('change_4h'),
            capital_total=spot_total_capital + futures_total,
            spot_balance=spot_balance,
            futures_balance=futures_balance,
            mode=mode,
            candidates=candidates,
        )
    except Exception:
        pass


def safe_persist_bot_state(
    state,
    btc_ctx=None,
    spot_real=None,
    futures_real=None,
    max_longs=None,
    max_shorts=None,
    system_health='OK',
    out_fn=None,
):
    path, error = bot_state.safe_persist_bot_state(
        state=state,
        btc_ctx=btc_ctx,
        spot_real=spot_real,
        futures_real=futures_real,
        max_longs=max_longs,
        max_shorts=max_shorts,
        system_health=system_health,
        bot_status='ONLINE' if system_health != 'ERROR' else 'UNKNOWN',
    )
    if error and out_fn:
        out_fn(f'BotState write warning: {error}')
    return path
