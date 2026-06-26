#!/usr/bin/env python3
"""
Orquestador principal del bot de trading.
Corre cada 10 min via cron. Gestiona longs (spot) y shorts (futures) simultÃ¡neamente.
"""
import sys, os, time, json
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass
sys.path.insert(0, os.path.dirname(__file__))
import config, utils, market, longs, shorts, rebalance, capital_manager
from analytics import AnalyticsLogger, DecisionSnapshotLogger
from telegram_alerts import send_telegram_alert

OUTPUT = []
ANALYTICS = AnalyticsLogger()
DECISIONS = DecisionSnapshotLogger()

def out(msg):
    print(msg)
    OUTPUT.append(msg)


def _safe_log_open(pos, candidate, btc_ctx, capital_at_entry):
    try:
        ANALYTICS.log_trade_open(
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
            capital_at_entry=capital_at_entry,
        )
    except Exception:
        pass


def _safe_log_close(pos, exit_price, exit_reason, pnl):
    try:
        ANALYTICS.log_trade_close(
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


def _safe_log_decision_snapshot(btc_ctx, spot_total_capital, spot_balance, futures_balance):
    try:
        decisions = market.get_last_decision_candidates()
        candidates = decisions.get('long', []) + decisions.get('short', [])
        futures_total = utils.get_total_futures()
        mode = btc_ctx.get('force_mode') or ('directional' if config.DIRECTIONAL_MODE else 'both_sides')
        DECISIONS.log_snapshot(
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


def main():
    lock = utils.acquire_lock()
    if not lock:
        print('âš ï¸ Ya hay una instancia corriendo. Saliendo.')
        sys.exit(0)
    try:
        _run()
    except Exception as e:
        err = str(e)
        if '503' in err or '502' in err or '504' in err:
            print(f'\u26a0\ufe0f Binance no disponible temporalmente ({e}). El pr\u00f3ximo ciclo reintenta.')
        elif '429' in err or '418' in err:
            print(f'\u26a0\ufe0f Rate limit de Binance alcanzado. El pr\u00f3ximo ciclo reintenta.')
        else:
            print(f'\u274c Error inesperado: {e}')
            send_telegram_alert('CRITICAL', 'Bot error inesperado', str(e))
            import traceback; traceback.print_exc()
    finally:
        utils.release_lock(lock)


def _run():
    state = utils.load_state()

    # â”€â”€ Migrar cooldown_symbols de lista a dict si es necesario â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if isinstance(state.get('cooldown_symbols'), list):
        state['cooldown_symbols'] = {s: 0 for s in state['cooldown_symbols']}

    # â”€â”€ Reset diario â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    today = time.strftime('%Y-%m-%d', time.gmtime())
    if state.get('pnl_date') != today:
        if state.get('status') == 'paused':
            state['status'] = 'active'
            out('âœ… Nuevo dÃ­a â€” bot reactivado.')
        state['pnl_date']            = today
        state['daily_pnl_usdt']      = 0.0
        spot_free = utils.get_usdt_spot()
        spot_in_pos = sum(
            p['entry_price'] * p['quantity']
            for p in state.get('positions', []) if p['direction'] == 'long'
        )
        state['daily_start_capital'] = spot_free + spot_in_pos + utils.get_total_futures()
        state['consec_sl']           = 0

    # â”€â”€ AuditorÃ­a: activos huÃ©rfanos â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    _audit_orphans(state)

    # â”€â”€ RevisiÃ³n blacklist dinÃ¡mica (cada 6h) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    last_bl_review = state.get('last_bl_review', 0)
    if time.time() - last_bl_review > 21600:  # 6 horas
        rehabilitated = market.review_dynamic_blacklist()
        if rehabilitated:
            out(f'âœ… Rehabilitados desde blacklist: {", ".join(rehabilitated)}')
        state['last_bl_review'] = int(time.time())

    # â”€â”€ Pausa global â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if state.get('status') == 'paused':
        out(f'â¸ï¸ Bot pausado (lÃ­mite diario). PnL hoy: {state.get("daily_pnl_usdt", 0):+.4f} USDT')
        utils.save_state(state)
        return

    # â”€â”€ Circuit breaker: pausa 24h si â‰¥4 SLs consecutivos â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if state.get('consec_sl', 0) >= 4:
        state['status'] = 'paused'
        state['pause_until'] = int(time.time()) + 86400  # 24h
        out('â›” Circuit breaker: 4 SLs consecutivos â†’ bot pausado por 24h')
        utils.send_alert('â›” Bot pausado por circuit breaker: 4 SLs consecutivos')
        try:
            ANALYTICS.log_event(
                'CIRCUIT_BREAKER',
                consec_sl=state.get('consec_sl', 0),
                pause_until=state.get('pause_until'),
                status=state.get('status'),
            )
        except Exception:
            pass
        utils.save_state(state)
        return

    # â”€â”€ Verificar si pausa por circuit breaker ya expirÃ³ â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    pause_until = state.get('pause_until', 0)
    if pause_until > 0 and int(time.time()) < pause_until:
        remaining_h = (pause_until - int(time.time())) / 3600
        out(f'â¸ï¸ Bot pausado (circuit breaker). Restan {remaining_h:.1f}h')
        utils.save_state(state)
        return
    elif pause_until > 0 and int(time.time()) >= pause_until:
        # Pausa expirÃ³, reactivar
        state['status'] = 'active'
        state['pause_until'] = 0
        state['consec_sl'] = 0
        out('âœ… Circuit breaker expirado â†’ bot reactivado')
        utils.save_state(state)

    # â”€â”€ MÃ¡ximo de posiciones abiertas simultÃ¡neas â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    MAX_OPEN_POSITIONS = 3  # con capital ~$50, no diversificar en exceso
    active_positions = state.get('positions', [])
    if len(active_positions) >= MAX_OPEN_POSITIONS:
        out(f'â¸ï¸ MÃ¡ximo de posiciones abiertas ({MAX_OPEN_POSITIONS}). Esperando cierres.')
        # No retornar â€” igual gestionar posiciones existentes

    # â”€â”€ Contexto de mercado (una sola vez) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    btc_ctx = market.get_btc_context()
    trend   = btc_ctx['trend']
    chg4h   = btc_ctx['change_4h']
    force   = btc_ctx.get('force_mode')

    ctx_emoji = 'ðŸŸ¢' if trend == 'bullish' else ('ðŸ”´' if trend == 'bearish' else 'ðŸŸ¡')
    out(f'{ctx_emoji} Contexto BTC: {trend.upper()} | Precio: ${btc_ctx["btc_price"]:.0f} | 4h: {chg4h:+.2f}%')
    if force:
        out(f'âš¡ Modo forzado: {force}')

    # â”€â”€ Rebalanceo de capital segÃºn contexto â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    rb_ok, rb_msg = rebalance.rebalance(state, btc_ctx)
    if rb_ok:
        out(rb_msg)
        utils.send_alert(rb_msg)

    # â”€â”€ 1. GESTIONAR posiciones activas â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # â”€â”€ 1a. Cierre preventivo por momentum extremo de BTC â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    close_shorts, close_longs, close_reason = market.check_btc_momentum_close(btc_ctx)
    if close_shorts or close_longs:
        out(f'ðŸš¨ {close_reason}')
        utils.send_alert(close_reason)
        
        # Cerrar posiciones afectadas
        for pos in active_positions[:]:
            direction = pos['direction']
            sym = pos['symbol']
            
            should_close = (direction == 'short' and close_shorts) or (direction == 'long' and close_longs)
            if should_close:
                # Cerrar al mercado
                if direction == 'short':
                    price_now = utils.get_fut_price(sym)
                    pnl = (pos['entry_price'] - price_now) * pos['quantity']
                    # Cancelar TP si existe
                    if pos.get('tp_order_id'):
                        try:
                            utils.fut_signed('DELETE', '/fapi/v1/order', {'symbol': sym, 'orderId': int(pos['tp_order_id'])})
                        except: pass
                else:
                    price_now = utils.get_spot_price(sym)
                    pnl = (price_now - pos['entry_price']) * pos['quantity']
                    # Cancelar OCO si existe
                    if pos.get('oco_id'):
                        try:
                            utils.spot_signed('DELETE', '/api/v3/orderList', {'symbol': sym, 'orderListId': int(pos['oco_id'])})
                        except: pass
                
                # Remover de posiciones
                out(f'  ðŸ”´ {sym} {direction}: cierre preventivo PnL={pnl:+.2f}')
                _safe_log_close(pos, price_now, 'PREVENTIVE_BTC_MOMENTUM', pnl)
                active_positions.remove(pos)
                
                # Actualizar PnL
                state['total_pnl_usdt'] = state.get('total_pnl_usdt', 0) + pnl
                state['daily_pnl_usdt'] = state.get('daily_pnl_usdt', 0) + pnl
                
                # Loggear trade
                now = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())
                try:
                    with open(config.TRADES_LOG, encoding='utf-8') as existing_log:
                        trade_id = sum(1 for _ in existing_log) + 1
                    with open(config.TRADES_LOG, 'a', encoding='utf-8') as f:
                        label = 'PREVENTIVO'
                        direction_tag = 'L' if direction == 'long' else 'S'
                        f.write(f'{trade_id:3d}  | {direction_tag} {sym.replace("USDT","")}/USDT | {label:13} | {pnl:+.4f}    | ${state["total_pnl_usdt"]:.4f}   | {now}\n')
                except Exception as e:
                    out(f'Log trade write failed: {e}')
        
        # Recargar lista despuÃ©s de cierres
        active_positions = state.get('positions', [])
    
    # â”€â”€ 1b. GestiÃ³n normal de posiciones restantes â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    positions_to_keep = []

    for pos in active_positions:
        direction = pos['direction']
        sym       = pos['symbol']

        if direction == 'long':
            # Chequear take profit parcial antes de la gestiÃ³n normal
            _check_partial_long(pos, state)
            action, price_close, pnl = longs.manage_long(pos, state)
        else:
            _check_partial_short(pos, state)
            action, price_close, pnl = shorts.manage_short(pos, state)

        if action in ('closed_tp', 'closed_sl', 'closed_manual'):
            _handle_close(state, pos, action, price_close, pnl, btc_ctx)
        elif action == 'updated':
            out(f'ðŸ”„ {direction.upper()} {sym} actualizado (trailing stop)')
            positions_to_keep.append(pos)
        else:
            # hold â€” mostrar estado
            if direction == 'short':
                upnl = (pos['entry_price'] - price_close) * pos['quantity']
                sl_dist = (pos['sl'] - price_close) / price_close * 100
                out(f'  ðŸ“‰ {sym}: ${price_close:.4f} | uPnL: {upnl:+.4f} | SL dist: {sl_dist:.2f}%')
            else:
                upnl = (price_close - pos['entry_price']) * pos['quantity']
                out(f'  ðŸ“ˆ {sym}: ${price_close:.4f} | uPnL: {upnl:+.4f}')
            positions_to_keep.append(pos)

    state['positions'] = positions_to_keep

    if state.get('status') == 'paused':
        utils.save_state(state)
        return

    # â”€â”€ 2. EVALUAR nuevas entradas â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    active_symbols = {p['symbol'] for p in positions_to_keep}
    cooldowns      = utils.get_active_cooldowns(state)
    excluded       = active_symbols | cooldowns
    long_count     = sum(1 for p in positions_to_keep if p['direction'] == 'long')
    short_count    = sum(1 for p in positions_to_keep if p['direction'] == 'short')
    spot_free      = utils.get_usdt_spot()
    spot_in_positions = sum(
        p['entry_price'] * p['quantity']
        for p in positions_to_keep if p['direction'] == 'long'
    )
    spot_total_capital = spot_free + spot_in_positions
    fut_free       = utils.get_usdt_futures()
    max_longs      = utils.get_max_long_positions(spot_total_capital)
    max_shorts     = utils.get_max_short_positions(fut_free)

    # Mostrar cooldowns activos si hay
    if cooldowns:
        cd_info = state.get('cooldown_symbols', {})
        now = int(time.time())
        cd_strs = []
        for sym in cooldowns:
            expiry = cd_info.get(sym, 0) if isinstance(cd_info, dict) else 0
            rem_h  = max(0, (expiry - now) / 3600) if expiry else 0
            cd_strs.append(f'{sym}({rem_h:.1f}h)')
        out(f'â³ Cooldown: {", ".join(cd_strs)}')

    # â”€â”€ Pausa post-SL: saltar entradas por 2 ciclos â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    skip_cycles = state.get('skip_next_cycles', 0)
    if skip_cycles > 0:
        state['skip_next_cycles'] = skip_cycles - 1
        out(f'â¸ï¸ Pausa post-SL: saltando ciclo de entradas ({skip_cycles} restantes)')
        # No retornar â€” igual gestionar posiciones existentes, solo no abrir nuevas
        skip_new_entries = True
    else:
        skip_new_entries = False

    market.reset_decision_candidates()

    # â”€â”€ 2a. LONGS â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if long_count < max_longs and force != 'short_only' and not skip_new_entries:
        best_long, descarte_long = market.scan_longs(btc_ctx, excluded_symbols=excluded)
        utils.log_analysis('long', best_long, descarte_long)

        if best_long:
            out(f'ðŸ” Candidato LONG: {best_long["symbol"]} score={best_long["score"]} RSI={best_long["rsi"]:.0f} reasons={best_long["reasons"]}')
            pos, msg = longs.open_long(best_long, state)
            if pos:
                state['positions'].append(pos)
                _safe_log_open(pos, best_long, btc_ctx, spot_total_capital)
                out(msg)
                utils.send_alert(msg)
            else:
                out(f'âš ï¸ LONG no abierto: {msg}')
                utils.send_alert(f'âš ï¸ FALLÃ“ apertura LONG {best_long["symbol"]}: {msg}')
                # Log detallado para debugging
                import logging
                logging.error(f'LONG fallido {best_long["symbol"]}: {msg}')
        else:
            motivo = descarte_long.get('MERCADO', 'sin candidatos vÃ¡lidos')
            # No mostrar en consola si es por modo direccional (ya estÃ¡ en config)
            if 'modo direccional' not in motivo:
                out(f'ðŸ” LONG: sin entrada ({motivo})')

    # â”€â”€ 2b. SHORTS â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if short_count < max_shorts and force != 'long_only' and not skip_new_entries:
        excl_short = {p['symbol'] for p in state['positions']} | cooldowns
        best_short, descarte_short = market.scan_shorts(btc_ctx, excluded_symbols=excl_short)
        utils.log_analysis('short', best_short, descarte_short)

        if best_short:
            out(f'ðŸ” Candidato SHORT: {best_short["symbol"]} score={best_short["score"]} RSI={best_short["rsi"]:.0f} reasons={best_short["reasons"]}')
            pos, msg = shorts.open_short(best_short, state)
            if pos:
                state['positions'].append(pos)
                _safe_log_open(pos, best_short, btc_ctx, fut_free)
                out(msg)
                utils.send_alert(msg)
            else:
                out(f'âš ï¸ SHORT no abierto: {msg}')
                utils.send_alert(f'âš ï¸ FALLÃ“ apertura SHORT {best_short["symbol"]}: {msg}')
                # Log detallado para debugging
                import logging
                logging.error(f'SHORT fallido {best_short["symbol"]}: {msg}')
        else:
            motivo = descarte_short.get('MERCADO', 'sin candidatos vÃ¡lidos')
            # No mostrar en consola si es por modo direccional (ya estÃ¡ en config)
            if 'modo direccional' not in motivo:
                out(f'ðŸ” SHORT: sin entrada ({motivo})')

    _safe_log_decision_snapshot(btc_ctx, spot_total_capital, spot_free, fut_free)

    # â”€â”€ Resumen â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Recalcular contadores reales (pueden haber cambiado si se abrio nueva posicion)
    long_count_final  = sum(1 for p in state['positions'] if p['direction'] == 'long')
    short_count_final = sum(1 for p in state['positions'] if p['direction'] == 'short')
    spot_bal  = utils.get_usdt_spot()
    # Total spot = USDT libre + valor de longs abiertos
    spot_in_positions = sum(
        p['entry_price'] * p['quantity']
        for p in state['positions'] if p['direction'] == 'long'
    )
    spot_total = round(spot_bal + spot_in_positions, 2)
    spot_used  = round(spot_in_positions, 2)
    fut_total, fut_avail, fut_margin = utils.get_futures_summary()
    # Valor nocional de posiciones short activas
    short_notional = sum(
        p['entry_price'] * p['quantity'] / p.get('leverage', config.FUTURES_LEVERAGE)
        for p in state['positions'] if p['direction'] == 'short'
    )
    try:
        cap = capital_manager.snapshot(spot_total, fut_total)
        out(
            f'Capital limits: Spot real ${cap["spot_real"]:.2f} usable ${cap["spot_usable"]:.2f} | '
            f'Futures real ${cap["futures_real"]:.2f} usable ${cap["futures_usable"]:.2f} | '
            f'Max pos {cap["max_position_percent"]:.2f}% | Max exposure {cap["max_exposure_percent"]:.2f}%'
        )
    except Exception as e:
        out(f'Capital limits: ERROR ({e})')
    out(f'\nðŸ’¼ Longs: {long_count_final}/{max_longs} | Shorts: {short_count_final}/{max_shorts} | Spot: ${spot_used:.2f}/${spot_total:.2f} | Futures: ${short_notional:.2f}/${fut_total:.2f}')
    out(f'ðŸ“Š PnL total: {state["total_pnl_usdt"]:+.4f} USDT | Hoy: {state["daily_pnl_usdt"]:+.4f} USDT')

    # â”€â”€ Limpieza semanal de polvo â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    _maybe_clean_dust(state)

    utils.save_state(state)


# â”€â”€ Helpers internos â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _recolocar_oco_long(pos, sym, qty_total, step, price, tp, entry):
    """Recoloca OCO de emergencia cuando el parcial falla pero el OCO ya fue cancelado."""
    import urllib.error as _ue
    try:
        tick     = utils.get_spot_filters(sym).get('tick_size', 0.0001)
        qty      = utils.round_step(qty_total, step)
        new_sl   = utils.round_tick(entry * (1 - config.SL_MIN_DIST_PCT / 100), tick)
        new_sl_l = utils.round_tick(new_sl * 0.999, tick)
        new_tp   = utils.round_tick(tp, tick)
        if qty * price < 5.0:
            utils.send_alert(f'ðŸš¨ {sym}: no pude recolocar OCO (qty insuficiente). RevisiÃ³n manual requerida.')
            return
        oco = utils.spot_signed('POST', '/api/v3/order/oco', {
            'symbol': sym, 'side': 'SELL', 'quantity': str(qty),
            'price': str(new_tp), 'stopPrice': str(new_sl),
            'stopLimitPrice': str(new_sl_l), 'stopLimitTimeInForce': 'GTC',
        })
        pos['oco_order_list_id'] = str(oco.get('orderListId', ''))
        pos['oco_order_ids']     = [str(o['orderId']) for o in oco.get('orders', [])]
        pos['quantity']          = qty
        out(f'âœ… OCO recolocado para {sym} tras fallo de parcial')
    except _ue.HTTPError as e:
        err = utils._binance_error_msg(e)
        utils.send_alert(f'ðŸš¨ {sym}: no pude recolocar OCO ({err}). RevisiÃ³n manual requerida.')
    except Exception as e:
        utils.send_alert(f'ðŸš¨ {sym}: no pude recolocar OCO ({e}). RevisiÃ³n manual requerida.')


def _handle_close(state, pos, action, price_close, pnl, btc_ctx=None):
    """Procesa el cierre de una posiciÃ³n: actualiza estado, alerta, log."""
    sym       = pos['symbol']
    direction = pos['direction']

    state['trade_count']    = state.get('trade_count', 0) + 1
    state['total_pnl_usdt'] = round(state.get('total_pnl_usdt', 0) + pnl, 4)
    state['daily_pnl_usdt'] = round(state.get('daily_pnl_usdt', 0) + pnl, 4)
    spot_free_now = utils.get_usdt_spot()
    spot_in_pos_now = sum(
        p['entry_price'] * p['quantity']
        for p in state.get('positions', []) if p['direction'] == 'long'
    )
    capital_now = spot_free_now + spot_in_pos_now + utils.get_total_futures()

    label     = {'closed_tp': 'TP âœ…', 'closed_sl': 'SL ðŸ”´', 'closed_manual': 'STALE â±ï¸ (sin movimiento)'}[action]
    dir_emoji = 'ðŸ“ˆ' if direction == 'long' else 'ðŸ“‰'
    if action == 'closed_sl' and not pos.get('partial_taken'):
        msg = (
            f'{dir_emoji} {direction.upper()} {sym} cerrado: {label}\n'
            f'PnL: {pnl:+.4f} USDT | Acumulado: {state["total_pnl_usdt"]:+.4f} USDT'
        )
    elif action == 'closed_sl' and pos.get('partial_taken'):
        ppnl = pos.get('partial_pnl')
        ppnl_str = f'+${ppnl:.4f}' if ppnl else 'ver log'
        msg = (
            f'{dir_emoji} {direction.upper()} {sym} cerrado: {label} (breakeven â€” parcial TP cobrado: {ppnl_str})\n'
            f'PnL esta mitad: {pnl:+.4f} USDT | Acumulado: {state["total_pnl_usdt"]:+.4f} USDT'
        )
    else:
        msg = (
            f'{dir_emoji} {direction.upper()} {sym} cerrado: {label}\n'
            f'PnL: {pnl:+.4f} USDT | Acumulado: {state["total_pnl_usdt"]:+.4f} USDT'
        )
    out(msg)
    utils.send_alert(msg)
    utils.log_trade(state['trade_count'], sym, direction, label, pnl, capital_now)
    reason = {'closed_tp': 'TP', 'closed_sl': 'SL', 'closed_manual': 'STALE_EXIT'}[action]
    _safe_log_close(pos, price_close, reason, pnl)

    if action == 'closed_sl':
        had_partial = pos.get('partial_taken', False)

        # SL despuÃ©s de parcial TP: el riesgo real ya estaba protegido (breakeven)
        # No suma al circuit breaker ni dispara pausa post-SL
        if not had_partial:
            state['consec_sl'] = state.get('consec_sl', 0) + 1
            state['last_sl_time'] = int(time.time())
            state['skip_next_cycles'] = 2  # saltar 2 ciclos de entrada (~20 min)
        else:
            # Parcial previo â†’ SL es en realidad breakeven, no una pÃ©rdida real
            # Solo resetear racha si venÃ­a de SLs limpios (no acumular)
            # No sumar al consec_sl, no pausar
            pass

        if config.COOLDOWN_AFTER_SL:
            utils.add_cooldown(state, sym)

        # Auto-blacklist: solo contar SLs sin parcial previo (pÃ©rdidas reales)
        if not had_partial:
            sl_history = state.get('sl_history_by_symbol', {})
            if sym not in sl_history:
                sl_history[sym] = []
            sl_history[sym].append(int(time.time()))
            now = int(time.time())
            cutoff = now - 432000
            sl_history[sym] = [ts for ts in sl_history[sym] if ts > cutoff]
            state['sl_history_by_symbol'] = sl_history
            if len(sl_history[sym]) >= 3:
                if sym not in config.BLACKLIST_SYMBOLS:
                    config.BLACKLIST_SYMBOLS.add(sym)
                    out(f'â›” {sym} auto-blacklisted: 3 SLs reales en 5 dÃ­as')
                    utils.send_alert(f'â›” {sym} agregado a BLACKLIST automÃ¡tica: 3 SLs reales en 5 dÃ­as')
    else:
        state['consec_sl'] = 0
        utils.remove_cooldown(state, sym)

    # Verificar lÃ­mite de pÃ©rdida diaria
    daily_start = state.get('daily_start_capital', capital_now)
    if daily_start > 0:
        daily_loss_pct = (state['daily_pnl_usdt'] / daily_start) * 100
        if daily_loss_pct <= -config.DAILY_LOSS_LIMIT_PCT:
            state['status'] = 'paused'
            out(f'â›” LÃ­mite diario alcanzado ({daily_loss_pct:.2f}%). Bot pausado hasta maÃ±ana.')
            utils.send_alert(f'â›” Bot pausado por lÃ­mite diario: {daily_loss_pct:.2f}%')

    # Rebalanceo post-cierre: aprovechar el capital reciÃ©n liberado
    # Si la tendencia cambiÃ³ y habÃ­a posiciones viejas bloqueando la transferencia,
    # este es el momento de mover el capital disponible hacia la wallet correcta.
    try:
        rb_ok, rb_msg = rebalance.rebalance(state, btc_ctx)
        if rb_ok:
            out(rb_msg)
            utils.send_alert(rb_msg)
    except Exception:
        pass  # silencioso si falla, el ciclo principal lo reintenta


def _check_partial_long(pos, state):
    """
    Take profit parcial para longs:
    Si el precio alcanzÃ³ el 50% del recorrido hacia el TP â†’ vender 50% y mover SL a breakeven.
    """
    if pos.get('partial_taken'):
        return

    entry = pos['entry_price']
    tp    = pos['tp']
    sym   = pos['symbol']

    try:
        price = utils.get_spot_price(sym)
    except Exception:
        return

    mid = entry + (tp - entry) * config.PARTIAL_TAKE_PCT
    if price < mid:
        return

    # Cancelar OCO actual y vender 50%
    oco_id = pos.get('oco_order_list_id', '')
    qty_half = utils.round_step(pos['quantity'] * 0.5,
                                utils.get_spot_filters(sym).get('step_size', 0.001))
    qty_rest = utils.round_step(pos['quantity'] * 0.5,
                                utils.get_spot_filters(sym).get('step_size', 0.001))

    if qty_half * price < 5.0:   # notional mÃ­nimo
        return

    import urllib.error as _ue
    try:
        # Cancelar OCO â€” si ya se ejecutÃ³, manejar el error
        oco_cancelled = False
        if oco_id:
            try:
                utils.spot_signed('DELETE', '/api/v3/orderList', {'symbol': sym, 'orderListId': int(oco_id)})
                oco_cancelled = True
            except _ue.HTTPError as e:
                err = utils._binance_error_msg(e)
                if '-2011' in err or '-1013' in err:
                    # OCO ya ejecutado (TP o SL disparado) â€” no hay nada que vender
                    pos['partial_taken'] = True
                    out(f'âš ï¸ Parcial LONG {sym}: OCO ya ejecutado ({err}), marcando partial_taken')
                    return
                else:
                    out(f'âš ï¸ Parcial LONG {sym}: error al cancelar OCO ({err}), abortando parcial')
                    return

        # Verificar balance real antes de vender
        try:
            acct = utils.get_spot_account()
            base_asset = sym.replace('USDT', '')
            free_base = next((float(b['free']) for b in acct.get('balances', []) if b['asset'] == base_asset), 0)
            step = utils.get_spot_filters(sym).get('step_size', 0.001)
            qty_half_real = utils.round_step(min(qty_half, free_base * 0.5), step)
            qty_rest_real = utils.round_step(free_base - qty_half_real, step)
            if qty_half_real * price < 5.0 or qty_rest_real * price < 5.0:
                out(f'âš ï¸ Parcial LONG {sym}: qty insuficiente (free={free_base:.4f}), abortando')
                if oco_cancelled:
                    _recolocar_oco_long(pos, sym, free_base, step, price, tp, entry)
                return
        except Exception as e:
            out(f'âš ï¸ Parcial LONG {sym}: no pude verificar balance ({e}), abortando')
            return

        # Vender mitad
        try:
            utils.spot_signed('POST', '/api/v3/order', {
                'symbol': sym, 'side': 'SELL', 'type': 'MARKET', 'quantity': str(qty_half_real)
            })
        except _ue.HTTPError as e:
            err = utils._binance_error_msg(e)
            out(f'âš ï¸ Parcial LONG {sym}: venta fallida ({err})')
            if oco_cancelled:
                _recolocar_oco_long(pos, sym, qty_half_real + qty_rest_real, step, price, tp, entry)
            return

        pnl_partial = (price - entry) * qty_half_real
        tick = utils.get_spot_filters(sym).get('tick_size', 0.0001)

        # Nuevo OCO con SL en breakeven (entrada) y mismo TP
        new_sl       = utils.round_tick(entry * 1.003, tick)
        new_sl_limit = utils.round_tick(new_sl * 0.999, tick)
        new_tp       = utils.round_tick(tp, tick)

        try:
            oco = utils.spot_signed('POST', '/api/v3/order/oco', {
                'symbol':               sym,
                'side':                 'SELL',
                'quantity':             str(qty_rest_real),
                'price':                str(new_tp),
                'stopPrice':            str(new_sl),
                'stopLimitPrice':       str(new_sl_limit),
                'stopLimitTimeInForce': 'GTC',
            })
        except _ue.HTTPError as e:
            err = utils._binance_error_msg(e)
            utils.send_alert(f'ðŸš¨ Parcial LONG {sym}: vendido pero OCO fallido ({err}). IntervenciÃ³n requerida.')
            out(f'ðŸš¨ Parcial LONG {sym}: vendido 50% pero no pude colocar nuevo OCO ({err})')
            pos['partial_taken'] = True
            return

        pos['quantity']            = qty_rest_real
        pos['sl']                  = new_sl
        pos['oco_order_list_id']   = str(oco.get('orderListId', ''))
        pos['oco_order_ids']       = [str(o['orderId']) for o in oco.get('orders', [])]
        pos['partial_taken']       = True
        pos['partial_pnl']         = round(pnl_partial, 4)

        msg = (
            f'ðŸ’° PARCIAL LONG {sym}: vendÃ­ 50% @ ${price:.4f}\n'
            f'PnL parcial: +${pnl_partial:.4f} | SL movido a breakeven ${new_sl:.4f}'
        )
        out(msg)
        utils.send_alert(msg)
        state['total_pnl_usdt'] = round(state.get('total_pnl_usdt', 0) + pnl_partial, 4)
        state['daily_pnl_usdt'] = round(state.get('daily_pnl_usdt', 0) + pnl_partial, 4)
        try:
            ANALYTICS.log_trade_close(
                trade_id=f'{pos.get("id")}:partial',
                symbol=sym,
                side='LONG',
                entry_time=pos.get('entry_time'),
                entry_price=entry,
                exit_price=price,
                exit_reason='PARTIAL_TP',
                pnl_usdt=pnl_partial,
            )
        except Exception:
            pass

    except Exception as e:
        out(f'âš ï¸ Parcial LONG {sym} error inesperado: {e}')


def _check_partial_short(pos, state):
    """
    Take profit parcial para shorts:
    Si el precio bajÃ³ el 50% hacia el TP â†’ cerrar 50% con MARKET y mover SL a breakeven.
    """
    if pos.get('partial_taken'):
        return

    entry = pos['entry_price']
    tp    = pos['tp']
    sym   = pos['symbol']

    try:
        price = utils.get_fut_price(sym)
    except Exception:
        return

    mid = entry - (entry - tp) * config.PARTIAL_TAKE_PCT
    if price > mid:
        return

    qty      = pos['quantity']
    step     = utils.get_futures_filters(sym).get('step_size', 0.01)
    qty_half = utils.round_step(qty * 0.5, step)
    qty_rest = utils.round_step(qty * 0.5, step)

    if qty_half < utils.get_futures_filters(sym).get('min_qty', 0.01):
        return

    try:
        # Cerrar mitad con MARKET
        order = utils.fut_signed('POST', '/fapi/v1/order', {
            'symbol': sym, 'side': 'BUY', 'type': 'MARKET',
            'quantity': str(qty_half), 'reduceOnly': 'true',
        })
        time.sleep(1)
        d = utils.fut_signed('GET', '/fapi/v1/order', {
            'symbol': sym, 'orderId': order['orderId']
        })
        fill = float(d.get('avgPrice', price))
        if fill == 0:
            fill = price

        pnl_partial = (entry - fill) * qty_half

        # Cancelar TP viejo y poner nuevo para la mitad restante
        tp_id = pos.get('tp_order_id', '')
        if tp_id:
            try:
                utils.fut_signed('DELETE', '/fapi/v1/order', {'symbol': sym, 'orderId': int(tp_id)})
            except Exception:
                pass

        tick   = utils.get_futures_filters(sym).get('tick_size', 0.001)
        # Fix #2: breakeven para SHORT es por ENCIMA de la entrada (SL se activa cuando precio SUBE)
        new_sl = utils.round_tick(entry * 1.003, tick)   # breakeven + 0.3% (cubre fees + ruido)
        new_tp = utils.round_tick(tp, tick)

        tp_order = utils.fut_signed('POST', '/fapi/v1/order', {
            'symbol': sym, 'side': 'BUY', 'type': 'LIMIT',
            'price': str(new_tp), 'quantity': str(qty_rest),
            'reduceOnly': 'true', 'timeInForce': 'GTC',
        })
        new_tp_order_id = str(tp_order.get('orderId', ''))

        # Fix #1: actualizar SL nativo en el exchange con la nueva qty y nuevo precio
        old_sl_id = pos.get('sl_order_id', '')
        new_sl_order_id = ''
        if config.NATIVE_SL_ENABLED:
            # Cancelar SL nativo viejo (tenÃ­a qty_total)
            if old_sl_id:
                try:
                    utils.fut_signed('DELETE', '/fapi/v1/order', {
                        'symbol': sym, 'orderId': int(old_sl_id)
                    })
                except Exception:
                    pass
            # Colocar nuevo SL nativo con qty_rest y precio breakeven
            # Solo colocar STOP_MARKET si el stopPrice estÃ¡ POR ENCIMA del precio actual.
            # Si el precio ya bajÃ³ mÃ¡s allÃ¡ del breakeven, Binance rechaza la orden (400).
            # En ese caso el guardian software es suficiente â€” el SL ya no tiene sentido colocarlo.
            try:
                price_now = utils.get_fut_price(sym)
                if new_sl > price_now * 1.0005:  # margen mÃ­nimo de 0.05% sobre precio actual
                    # ValidaciÃ³n adicional: stopPrice no debe exceder ~4.5% del precio actual
                    # Binance rechaza STOP_MARKET si stopPrice > markPrice +5%
                    max_stop_dist_pct = 4.5
                    max_allowed_sl = price_now * (1 + max_stop_dist_pct / 100)
                    if new_sl > max_allowed_sl:
                        new_sl = utils.round_tick(max_allowed_sl, tick)
                    
                    sl_order = shorts._place_stop_market(sym, 'BUY', new_sl, qty_rest)
                    new_sl_order_id = str(sl_order.get('orderId', '') or sl_order.get('strategyId', '')) if sl_order else ''
                else:
                    # Precio ya por debajo del breakeven â€” guardian software cubre
                    pass
            except Exception as e:
                import logging
                error_msg = str(e)
                logging.error(f'SL breakeven {sym}: stopPrice={new_sl}, qty={qty_rest}, price={price_now}, error={error_msg}')
                utils.send_alert(f'âš ï¸ SL nativo breakeven {sym} no se pudo colocar: {error_msg}. Guardian software activo.')

        pos['quantity']      = qty_rest
        pos['sl']            = new_sl
        pos['tp_order_id']   = new_tp_order_id
        pos['sl_order_id']   = new_sl_order_id
        pos['partial_taken'] = True
        pos['partial_pnl']   = round(pnl_partial, 4)

        msg = (
            f'ðŸ’° PARCIAL SHORT {sym}: cerrÃ© 50% @ ${fill:.4f}\n'
            f'PnL parcial: +${pnl_partial:.4f} | SL movido a breakeven ${new_sl:.4f}'
        )
        out(msg)
        utils.send_alert(msg)
        # Registrar PnL parcial en el state y en el log
        state['trade_count']    = state.get('trade_count', 0) + 1
        state['total_pnl_usdt'] = round(state.get('total_pnl_usdt', 0) + pnl_partial, 4)
        state['daily_pnl_usdt'] = round(state.get('daily_pnl_usdt', 0) + pnl_partial, 4)
        spot_free_now = utils.get_usdt_spot()
        capital_now   = spot_free_now + utils.get_total_futures()
        try:
            ANALYTICS.log_trade_close(
                trade_id=f'{pos.get("id")}:partial',
                symbol=sym,
                side='SHORT',
                entry_time=pos.get('entry_time'),
                entry_price=entry,
                exit_price=fill,
                exit_reason='PARTIAL_TP',
                pnl_usdt=pnl_partial,
            )
        except Exception:
            pass
        utils.log_trade(state['trade_count'], sym, 'short', 'PARCIAL TP ðŸ’° (50%)', pnl_partial, capital_now)

    except Exception as e:
        out(f'âš ï¸ Parcial SHORT {sym} fallÃ³: {e}')


def _audit_orphans(state):
    """
    Detecta activos spot con valor > $5 que no tienen posiciÃ³n registrada en el state.
    Si encuentra uno: intenta colocar un OCO de protecciÃ³n y lo agrega al state.
    Evita que un activo quede desprotegido por ciclos duplicados o errores de escritura.

    Fix #7: excluye falsos positivos:
    - Activos en cooldown (el par USDT estÃ¡ en cooldown_symbols) â†’ solo monitoreo, no OCO
    - Activos en proceso de limpieza de polvo (dust_in_progress) â†’ ignorar
    - Activos sin par USDT en futures (no tradeable como short) pero sÃ­ en spot
    """
    try:
        active_syms = {p['symbol'] for p in state.get('positions', []) if p['direction'] == 'long'}
        # Fix #7a: obtener cooldowns activos para excluirlos de la recuperaciÃ³n automÃ¡tica
        cooldown_syms = utils.get_active_cooldowns(state)
        dust_in_progress = state.get('dust_in_progress', False)

        # Precios en batch
        import urllib.request as _ur
        all_prices = {}
        try:
            r = _ur.urlopen(f'{config.SPOT_BASE}/api/v3/ticker/price', timeout=8)
            for p in json.loads(r.read()):
                all_prices[p['symbol']] = float(p['price'])
        except Exception:
            pass

        acc = utils.get_spot_account()
        for b in acc.get('balances', []):
            asset  = b['asset']
            free   = float(b['free'])
            locked = float(b['locked'])
            total  = free + locked
            if asset in config.DUST_PROTECTED or total < 0.001:
                continue
            # Si hay cantidad bloqueada es porque hay una orden activa (OCO, limit) â†’ no es huÃ©rfano
            if locked > 0:
                continue
            sym    = asset + 'USDT'
            price  = all_prices.get(sym, 0)
            if price == 0 or total * price < 5.0:
                continue
            if sym in active_syms:
                continue

            # Fix #7b: si el par estÃ¡ en cooldown, no es un huÃ©rfano accionable â€” solo alertar
            if sym in cooldown_syms:
                cd_info = state.get('cooldown_symbols', {})
                expiry  = cd_info.get(sym, 0) if isinstance(cd_info, dict) else 0
                rem_h   = max(0, (expiry - int(time.time())) / 3600) if expiry else 0
                out(f'â„¹ï¸ {asset} en cooldown ({rem_h:.1f}h restantes), no se coloca OCO automÃ¡tico')
                continue

            # Fix #7c: si hay limpieza de polvo en progreso, ignorar activos pequeÃ±os
            # (pueden ser residuos de conversiones parciales que se limpiarÃ¡n solas)
            if dust_in_progress and total * price < 15.0:
                continue

            # Activo huÃ©rfano detectado
            msg = f'âš ï¸ Activo huÃ©rfano detectado: {asset} ({total:.4f} = ${total*price:.2f})'
            out(msg)
            utils.send_alert(msg)

            # Buscar precio de entrada en historial de trades
            try:
                trades = utils.spot_signed('GET', '/api/v3/myTrades', {'symbol': sym, 'limit': 5})
                buys = [t for t in trades if t['isBuyer']]
                entry = float(buys[-1]['price']) if buys else price
            except Exception:
                entry = price

            # Calcular SL/TP desde precio actual
            try:
                k1h    = utils.get_klines(sym, '1h', 50)
                closes = [float(k[4]) for k in k1h]
                highs  = [float(k[2]) for k in k1h]
                lows   = [float(k[3]) for k in k1h]
                trs    = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])) for i in range(1, len(closes))]
                atr    = sum(trs[-14:]) / 14
                cur    = closes[-1]
            except Exception:
                atr = price * 0.015
                cur = price

            try:
                filters   = utils.get_spot_filters(sym)
                tick      = filters.get('tick_size', 0.0001)
                step      = filters.get('step_size', 0.1)
                qty       = utils.round_step(free, step)  # solo qty libre

                sl        = utils.round_tick(cur - config.SL_ATR_MULT * atr, tick)
                tp        = utils.round_tick(entry + config.TP_ATR_MULT * atr, tick)
                sl        = max(sl, cur * (1 - config.SL_MIN_DIST_PCT / 100 * 1.05))
                sl        = utils.round_tick(sl, tick)
                sl_limit  = utils.round_tick(sl * 0.9985, tick)

                if tp <= cur or sl >= cur or qty <= 0:
                    raise ValueError(f'precios invÃ¡lidos: sl={sl} cur={cur} tp={tp} qty={qty}')

                oco = utils.spot_signed('POST', '/api/v3/order/oco', {
                    'symbol':               sym,
                    'side':                 'SELL',
                    'quantity':             str(qty),
                    'price':                str(tp),
                    'stopPrice':            str(sl),
                    'stopLimitPrice':       str(sl_limit),
                    'stopLimitTimeInForce': 'GTC',
                })
                oco_id = str(oco.get('orderListId', ''))

                state['positions'].append({
                    'id':                f'long_{sym}_recovered_{int(time.time())}',
                    'direction':         'long',
                    'symbol':            sym,
                    'entry_price':       entry,
                    'quantity':          qty,
                    'sl':                sl,
                    'tp':                tp,
                    'atr':               atr,
                    'oco_order_list_id': oco_id,
                    'entry_time':        int(time.time()),
                    'partial_taken':     False,
                    'trail_peak':        cur,
                })
                _safe_log_open(state['positions'][-1], None, None, None)
                utils.save_state(state)
                ok_msg = f'âœ… {asset} recuperado: OCO colocado (SL=${sl:.4f} TP=${tp:.4f})'
                out(ok_msg)
                utils.send_alert(ok_msg)

            except Exception as e:
                out(f'âŒ No se pudo proteger {asset}: {e}')
                utils.send_alert(f'ðŸš¨ {asset} huÃ©rfano sin OCO: {e}. Requiere intervenciÃ³n manual.')

    except Exception as e:
        out(f'âš ï¸ AuditorÃ­a fallÃ³: {e}')


def _maybe_clean_dust(state):
    """
    Convierte polvo a BNB de a un activo por ciclo (rate limit: 1/hora de Binance).
    La limpieza inicial arranca el lunes; luego sigue ciclo a ciclo hasta terminar.
    """
    import time as _time
    now     = int(_time.time())
    last    = state.get('last_dust_clean', 0)
    weekday = _time.gmtime(now).tm_wday

    # Arrancar limpieza si: es lunes Y pasÃ³ al menos 1 semana desde la Ãºltima
    # O si hay una limpieza en progreso (last_dust_clean es reciente pero no terminado)
    dust_in_progress = state.get('dust_in_progress', False)
    nueva_semana     = (weekday == config.DUST_CLEAN_DAY and now - last >= 604800)

    if not dust_in_progress and not nueva_semana:
        return

    # Rate limit: esperar al menos 61 min entre conversiones
    last_conv = state.get('last_dust_conversion', 0)
    if now - last_conv < 3660:
        return

    assets, msg = utils.clean_dust(dry_run=config.DRY_RUN)
    if assets:
        out(f'ðŸ§¹ Polvo: {msg}')
        utils.send_alert(f'ðŸ§¹ {msg}')
        state['last_dust_conversion'] = now
        state['dust_in_progress']     = True   # seguir el prÃ³ximo ciclo
    elif 'Rate limit' in msg:
        pass  # silencio, reintenta sÃ³lo
    elif 'Sin polvo' in msg or 'insuficiente' in msg:
        # TerminÃ³, limpiar flags
        state['last_dust_clean']      = now
        state['dust_in_progress']     = False
        if nueva_semana:
            out(f'ðŸ§¹ Limpieza de polvo completada')
    else:
        # NingÃºn activo convertÃ­ble esta vuelta, seguir intentando
        state['last_dust_conversion'] = now


if __name__ == '__main__':
    main()
