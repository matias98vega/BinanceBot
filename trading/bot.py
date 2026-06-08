#!/usr/bin/env python3
"""
Orquestador principal del bot de trading.
Corre cada 10 min via cron. Gestiona longs (spot) y shorts (futures) simultáneamente.
"""
import sys, os, time, json
sys.path.insert(0, os.path.dirname(__file__))
import config, utils, market, longs, shorts, rebalance

OUTPUT = []

def out(msg):
    print(msg)
    OUTPUT.append(msg)


def main():
    lock = utils.acquire_lock()
    if not lock:
        print('⚠️ Ya hay una instancia corriendo. Saliendo.')
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
            import traceback; traceback.print_exc()
    finally:
        utils.release_lock(lock)


def _run():
    state = utils.load_state()

    # ── Migrar cooldown_symbols de lista a dict si es necesario ─────────────
    if isinstance(state.get('cooldown_symbols'), list):
        state['cooldown_symbols'] = {s: 0 for s in state['cooldown_symbols']}

    # ── Reset diario ─────────────────────────────────────────────────────────
    today = time.strftime('%Y-%m-%d', time.gmtime())
    if state.get('pnl_date') != today:
        if state.get('status') == 'paused':
            state['status'] = 'active'
            out('✅ Nuevo día — bot reactivado.')
        state['pnl_date']            = today
        state['daily_pnl_usdt']      = 0.0
        spot_free = utils.get_usdt_spot()
        spot_in_pos = sum(
            p['entry_price'] * p['quantity']
            for p in state.get('positions', []) if p['direction'] == 'long'
        )
        state['daily_start_capital'] = spot_free + spot_in_pos + utils.get_total_futures()
        state['consec_sl']           = 0

    # ── Auditoría: activos huérfanos ─────────────────────────────────────────
    _audit_orphans(state)

    # ── Revisión blacklist dinámica (cada 6h) ────────────────────────────────
    last_bl_review = state.get('last_bl_review', 0)
    if time.time() - last_bl_review > 21600:  # 6 horas
        rehabilitated = market.review_dynamic_blacklist()
        if rehabilitated:
            out(f'✅ Rehabilitados desde blacklist: {", ".join(rehabilitated)}')
        state['last_bl_review'] = int(time.time())

    # ── Pausa global ─────────────────────────────────────────────────────────
    if state.get('status') == 'paused':
        out(f'⏸️ Bot pausado (límite diario). PnL hoy: {state.get("daily_pnl_usdt", 0):+.4f} USDT')
        utils.save_state(state)
        return

    # ── Circuit breaker: pausa 24h si ≥4 SLs consecutivos ─────────────────────
    if state.get('consec_sl', 0) >= 4:
        state['status'] = 'paused'
        state['pause_until'] = int(time.time()) + 86400  # 24h
        out('⛔ Circuit breaker: 4 SLs consecutivos → bot pausado por 24h')
        utils.send_alert('⛔ Bot pausado por circuit breaker: 4 SLs consecutivos')
        utils.save_state(state)
        return

    # ── Verificar si pausa por circuit breaker ya expiró ───────────────────────
    pause_until = state.get('pause_until', 0)
    if pause_until > 0 and int(time.time()) < pause_until:
        remaining_h = (pause_until - int(time.time())) / 3600
        out(f'⏸️ Bot pausado (circuit breaker). Restan {remaining_h:.1f}h')
        utils.save_state(state)
        return
    elif pause_until > 0 and int(time.time()) >= pause_until:
        # Pausa expiró, reactivar
        state['status'] = 'active'
        state['pause_until'] = 0
        state['consec_sl'] = 0
        out('✅ Circuit breaker expirado → bot reactivado')
        utils.save_state(state)

    # ── Máximo de posiciones abiertas simultáneas ──────────────────────────────
    MAX_OPEN_POSITIONS = 3  # con capital ~$50, no diversificar en exceso
    active_positions = state.get('positions', [])
    if len(active_positions) >= MAX_OPEN_POSITIONS:
        out(f'⏸️ Máximo de posiciones abiertas ({MAX_OPEN_POSITIONS}). Esperando cierres.')
        # No retornar — igual gestionar posiciones existentes

    # ── Contexto de mercado (una sola vez) ───────────────────────────────────
    btc_ctx = market.get_btc_context()
    trend   = btc_ctx['trend']
    chg4h   = btc_ctx['change_4h']
    force   = btc_ctx.get('force_mode')

    ctx_emoji = '🟢' if trend == 'bullish' else ('🔴' if trend == 'bearish' else '🟡')
    out(f'{ctx_emoji} Contexto BTC: {trend.upper()} | Precio: ${btc_ctx["btc_price"]:.0f} | 4h: {chg4h:+.2f}%')
    if force:
        out(f'⚡ Modo forzado: {force}')

    # ── Rebalanceo de capital según contexto ─────────────────────────────────
    rb_ok, rb_msg = rebalance.rebalance(state, btc_ctx)
    if rb_ok:
        out(rb_msg)
        utils.send_alert(rb_msg)

    # ── 1. GESTIONAR posiciones activas ──────────────────────────────────────
    # ── 1a. Cierre preventivo por momentum extremo de BTC ─────────────────────
    close_shorts, close_longs, close_reason = market.check_btc_momentum_close(btc_ctx)
    if close_shorts or close_longs:
        out(f'🚨 {close_reason}')
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
                out(f'  🔴 {sym} {direction}: cierre preventivo PnL={pnl:+.2f}')
                active_positions.remove(pos)
                
                # Actualizar PnL
                state['total_pnl_usdt'] = state.get('total_pnl_usdt', 0) + pnl
                state['daily_pnl_usdt'] = state.get('daily_pnl_usdt', 0) + pnl
                
                # Loggear trade
                now = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())
                with open(config.TRADES_LOG, 'a') as f:
                    trade_id = sum(1 for _ in open(config.TRADES_LOG)) + 1
                    label = 'PREVENTIVO 🚨'
                    f.write(f'{trade_id:3d}  | {"📈L" if direction=="long" else "📉S"} {sym.replace("USDT","")}/USDT | {label:13} | {pnl:+.4f}    | ${state["total_pnl_usdt"]:.4f}   | {now}\n')
        
        # Recargar lista después de cierres
        active_positions = state.get('positions', [])
    
    # ── 1b. Gestión normal de posiciones restantes ────────────────────────────
    positions_to_keep = []

    for pos in active_positions:
        direction = pos['direction']
        sym       = pos['symbol']

        if direction == 'long':
            # Chequear take profit parcial antes de la gestión normal
            _check_partial_long(pos, state)
            action, price_close, pnl = longs.manage_long(pos, state)
        else:
            _check_partial_short(pos, state)
            action, price_close, pnl = shorts.manage_short(pos, state)

        if action in ('closed_tp', 'closed_sl', 'closed_manual'):
            _handle_close(state, pos, action, price_close, pnl, btc_ctx)
        elif action == 'updated':
            out(f'🔄 {direction.upper()} {sym} actualizado (trailing stop)')
            positions_to_keep.append(pos)
        else:
            # hold — mostrar estado
            if direction == 'short':
                upnl = (pos['entry_price'] - price_close) * pos['quantity']
                sl_dist = (pos['sl'] - price_close) / price_close * 100
                out(f'  📉 {sym}: ${price_close:.4f} | uPnL: {upnl:+.4f} | SL dist: {sl_dist:.2f}%')
            else:
                upnl = (price_close - pos['entry_price']) * pos['quantity']
                out(f'  📈 {sym}: ${price_close:.4f} | uPnL: {upnl:+.4f}')
            positions_to_keep.append(pos)

    state['positions'] = positions_to_keep

    if state.get('status') == 'paused':
        utils.save_state(state)
        return

    # ── 2. EVALUAR nuevas entradas ────────────────────────────────────────────
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
        out(f'⏳ Cooldown: {", ".join(cd_strs)}')

    # ── Pausa post-SL: saltar entradas por 2 ciclos ───────────────────────────
    skip_cycles = state.get('skip_next_cycles', 0)
    if skip_cycles > 0:
        state['skip_next_cycles'] = skip_cycles - 1
        out(f'⏸️ Pausa post-SL: saltando ciclo de entradas ({skip_cycles} restantes)')
        # No retornar — igual gestionar posiciones existentes, solo no abrir nuevas
        skip_new_entries = True
    else:
        skip_new_entries = False

    # ── 2a. LONGS ─────────────────────────────────────────────────────────────
    if long_count < max_longs and force != 'short_only' and not skip_new_entries:
        best_long, descarte_long = market.scan_longs(btc_ctx, excluded_symbols=excluded)
        utils.log_analysis('long', best_long, descarte_long)

        if best_long:
            out(f'🔍 Candidato LONG: {best_long["symbol"]} score={best_long["score"]} RSI={best_long["rsi"]:.0f} reasons={best_long["reasons"]}')
            pos, msg = longs.open_long(best_long, state)
            if pos:
                state['positions'].append(pos)
                out(msg)
                utils.send_alert(msg)
            else:
                out(f'⚠️ LONG no abierto: {msg}')
                utils.send_alert(f'⚠️ FALLÓ apertura LONG {best_long["symbol"]}: {msg}')
                # Log detallado para debugging
                import logging
                logging.error(f'LONG fallido {best_long["symbol"]}: {msg}')
        else:
            motivo = descarte_long.get('MERCADO', 'sin candidatos válidos')
            # No mostrar en consola si es por modo direccional (ya está en config)
            if 'modo direccional' not in motivo:
                out(f'🔍 LONG: sin entrada ({motivo})')

    # ── 2b. SHORTS ────────────────────────────────────────────────────────────
    if short_count < max_shorts and force != 'long_only' and not skip_new_entries:
        excl_short = {p['symbol'] for p in state['positions']} | cooldowns
        best_short, descarte_short = market.scan_shorts(btc_ctx, excluded_symbols=excl_short)
        utils.log_analysis('short', best_short, descarte_short)

        if best_short:
            out(f'🔍 Candidato SHORT: {best_short["symbol"]} score={best_short["score"]} RSI={best_short["rsi"]:.0f} reasons={best_short["reasons"]}')
            pos, msg = shorts.open_short(best_short, state)
            if pos:
                state['positions'].append(pos)
                out(msg)
                utils.send_alert(msg)
            else:
                out(f'⚠️ SHORT no abierto: {msg}')
                utils.send_alert(f'⚠️ FALLÓ apertura SHORT {best_short["symbol"]}: {msg}')
                # Log detallado para debugging
                import logging
                logging.error(f'SHORT fallido {best_short["symbol"]}: {msg}')
        else:
            motivo = descarte_short.get('MERCADO', 'sin candidatos válidos')
            # No mostrar en consola si es por modo direccional (ya está en config)
            if 'modo direccional' not in motivo:
                out(f'🔍 SHORT: sin entrada ({motivo})')

    # ── Resumen ───────────────────────────────────────────────────────────────
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
    out(f'\n💼 Longs: {long_count_final}/{max_longs} | Shorts: {short_count_final}/{max_shorts} | Spot: ${spot_used:.2f}/${spot_total:.2f} | Futures: ${short_notional:.2f}/${fut_total:.2f}')
    out(f'📊 PnL total: {state["total_pnl_usdt"]:+.4f} USDT | Hoy: {state["daily_pnl_usdt"]:+.4f} USDT')

    # ── Limpieza semanal de polvo ─────────────────────────────────────────────────────
    _maybe_clean_dust(state)

    utils.save_state(state)


# ── Helpers internos ──────────────────────────────────────────────────────────

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
            utils.send_alert(f'🚨 {sym}: no pude recolocar OCO (qty insuficiente). Revisión manual requerida.')
            return
        oco = utils.spot_signed('POST', '/api/v3/order/oco', {
            'symbol': sym, 'side': 'SELL', 'quantity': str(qty),
            'price': str(new_tp), 'stopPrice': str(new_sl),
            'stopLimitPrice': str(new_sl_l), 'stopLimitTimeInForce': 'GTC',
        })
        pos['oco_order_list_id'] = str(oco.get('orderListId', ''))
        pos['oco_order_ids']     = [str(o['orderId']) for o in oco.get('orders', [])]
        pos['quantity']          = qty
        out(f'✅ OCO recolocado para {sym} tras fallo de parcial')
    except _ue.HTTPError as e:
        err = utils._binance_error_msg(e)
        utils.send_alert(f'🚨 {sym}: no pude recolocar OCO ({err}). Revisión manual requerida.')
    except Exception as e:
        utils.send_alert(f'🚨 {sym}: no pude recolocar OCO ({e}). Revisión manual requerida.')


def _handle_close(state, pos, action, price_close, pnl, btc_ctx=None):
    """Procesa el cierre de una posición: actualiza estado, alerta, log."""
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

    label     = {'closed_tp': 'TP ✅', 'closed_sl': 'SL 🔴', 'closed_manual': 'STALE ⏱️ (sin movimiento)'}[action]
    dir_emoji = '📈' if direction == 'long' else '📉'
    if action == 'closed_sl' and not pos.get('partial_taken'):
        msg = (
            f'{dir_emoji} {direction.upper()} {sym} cerrado: {label}\n'
            f'PnL: {pnl:+.4f} USDT | Acumulado: {state["total_pnl_usdt"]:+.4f} USDT'
        )
    elif action == 'closed_sl' and pos.get('partial_taken'):
        ppnl = pos.get('partial_pnl')
        ppnl_str = f'+${ppnl:.4f}' if ppnl else 'ver log'
        msg = (
            f'{dir_emoji} {direction.upper()} {sym} cerrado: {label} (breakeven — parcial TP cobrado: {ppnl_str})\n'
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

    if action == 'closed_sl':
        had_partial = pos.get('partial_taken', False)

        # SL después de parcial TP: el riesgo real ya estaba protegido (breakeven)
        # No suma al circuit breaker ni dispara pausa post-SL
        if not had_partial:
            state['consec_sl'] = state.get('consec_sl', 0) + 1
            state['last_sl_time'] = int(time.time())
            state['skip_next_cycles'] = 2  # saltar 2 ciclos de entrada (~20 min)
        else:
            # Parcial previo → SL es en realidad breakeven, no una pérdida real
            # Solo resetear racha si venía de SLs limpios (no acumular)
            # No sumar al consec_sl, no pausar
            pass

        if config.COOLDOWN_AFTER_SL:
            utils.add_cooldown(state, sym)

        # Auto-blacklist: solo contar SLs sin parcial previo (pérdidas reales)
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
                    out(f'⛔ {sym} auto-blacklisted: 3 SLs reales en 5 días')
                    utils.send_alert(f'⛔ {sym} agregado a BLACKLIST automática: 3 SLs reales en 5 días')
    else:
        state['consec_sl'] = 0
        utils.remove_cooldown(state, sym)

    # Verificar límite de pérdida diaria
    daily_start = state.get('daily_start_capital', capital_now)
    if daily_start > 0:
        daily_loss_pct = (state['daily_pnl_usdt'] / daily_start) * 100
        if daily_loss_pct <= -config.DAILY_LOSS_LIMIT_PCT:
            state['status'] = 'paused'
            out(f'⛔ Límite diario alcanzado ({daily_loss_pct:.2f}%). Bot pausado hasta mañana.')
            utils.send_alert(f'⛔ Bot pausado por límite diario: {daily_loss_pct:.2f}%')

    # Rebalanceo post-cierre: aprovechar el capital recién liberado
    # Si la tendencia cambió y había posiciones viejas bloqueando la transferencia,
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
    Si el precio alcanzó el 50% del recorrido hacia el TP → vender 50% y mover SL a breakeven.
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

    if qty_half * price < 5.0:   # notional mínimo
        return

    import urllib.error as _ue
    try:
        # Cancelar OCO — si ya se ejecutó, manejar el error
        oco_cancelled = False
        if oco_id:
            try:
                utils.spot_signed('DELETE', '/api/v3/orderList', {'symbol': sym, 'orderListId': int(oco_id)})
                oco_cancelled = True
            except _ue.HTTPError as e:
                err = utils._binance_error_msg(e)
                if '-2011' in err or '-1013' in err:
                    # OCO ya ejecutado (TP o SL disparado) — no hay nada que vender
                    pos['partial_taken'] = True
                    out(f'⚠️ Parcial LONG {sym}: OCO ya ejecutado ({err}), marcando partial_taken')
                    return
                else:
                    out(f'⚠️ Parcial LONG {sym}: error al cancelar OCO ({err}), abortando parcial')
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
                out(f'⚠️ Parcial LONG {sym}: qty insuficiente (free={free_base:.4f}), abortando')
                if oco_cancelled:
                    _recolocar_oco_long(pos, sym, free_base, step, price, tp, entry)
                return
        except Exception as e:
            out(f'⚠️ Parcial LONG {sym}: no pude verificar balance ({e}), abortando')
            return

        # Vender mitad
        try:
            utils.spot_signed('POST', '/api/v3/order', {
                'symbol': sym, 'side': 'SELL', 'type': 'MARKET', 'quantity': str(qty_half_real)
            })
        except _ue.HTTPError as e:
            err = utils._binance_error_msg(e)
            out(f'⚠️ Parcial LONG {sym}: venta fallida ({err})')
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
            utils.send_alert(f'🚨 Parcial LONG {sym}: vendido pero OCO fallido ({err}). Intervención requerida.')
            out(f'🚨 Parcial LONG {sym}: vendido 50% pero no pude colocar nuevo OCO ({err})')
            pos['partial_taken'] = True
            return

        pos['quantity']            = qty_rest_real
        pos['sl']                  = new_sl
        pos['oco_order_list_id']   = str(oco.get('orderListId', ''))
        pos['oco_order_ids']       = [str(o['orderId']) for o in oco.get('orders', [])]
        pos['partial_taken']       = True
        pos['partial_pnl']         = round(pnl_partial, 4)

        msg = (
            f'💰 PARCIAL LONG {sym}: vendí 50% @ ${price:.4f}\n'
            f'PnL parcial: +${pnl_partial:.4f} | SL movido a breakeven ${new_sl:.4f}'
        )
        out(msg)
        utils.send_alert(msg)
        state['total_pnl_usdt'] = round(state.get('total_pnl_usdt', 0) + pnl_partial, 4)
        state['daily_pnl_usdt'] = round(state.get('daily_pnl_usdt', 0) + pnl_partial, 4)

    except Exception as e:
        out(f'⚠️ Parcial LONG {sym} error inesperado: {e}')


def _check_partial_short(pos, state):
    """
    Take profit parcial para shorts:
    Si el precio bajó el 50% hacia el TP → cerrar 50% con MARKET y mover SL a breakeven.
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
            # Cancelar SL nativo viejo (tenía qty_total)
            if old_sl_id:
                try:
                    utils.fut_signed('DELETE', '/fapi/v1/order', {
                        'symbol': sym, 'orderId': int(old_sl_id)
                    })
                except Exception:
                    pass
            # Colocar nuevo SL nativo con qty_rest y precio breakeven
            # Solo colocar STOP_MARKET si el stopPrice está POR ENCIMA del precio actual.
            # Si el precio ya bajó más allá del breakeven, Binance rechaza la orden (400).
            # En ese caso el guardian software es suficiente — el SL ya no tiene sentido colocarlo.
            try:
                price_now = utils.get_fut_price(sym)
                if new_sl > price_now * 1.0005:  # margen mínimo de 0.05% sobre precio actual
                    # Validación adicional: stopPrice no debe exceder ~4.5% del precio actual
                    # Binance rechaza STOP_MARKET si stopPrice > markPrice +5%
                    max_stop_dist_pct = 4.5
                    max_allowed_sl = price_now * (1 + max_stop_dist_pct / 100)
                    if new_sl > max_allowed_sl:
                        new_sl = utils.round_tick(max_allowed_sl, tick)
                    
                    sl_order = shorts._place_stop_market(sym, 'BUY', new_sl, qty_rest)
                    new_sl_order_id = str(sl_order.get('orderId', '') or sl_order.get('strategyId', '')) if sl_order else ''
                else:
                    # Precio ya por debajo del breakeven — guardian software cubre
                    pass
            except Exception as e:
                import logging
                error_msg = str(e)
                logging.error(f'SL breakeven {sym}: stopPrice={new_sl}, qty={qty_rest}, price={price_now}, error={error_msg}')
                utils.send_alert(f'⚠️ SL nativo breakeven {sym} no se pudo colocar: {error_msg}. Guardian software activo.')

        pos['quantity']      = qty_rest
        pos['sl']            = new_sl
        pos['tp_order_id']   = new_tp_order_id
        pos['sl_order_id']   = new_sl_order_id
        pos['partial_taken'] = True
        pos['partial_pnl']   = round(pnl_partial, 4)

        msg = (
            f'💰 PARCIAL SHORT {sym}: cerré 50% @ ${fill:.4f}\n'
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
        utils.log_trade(state['trade_count'], sym, 'short', 'PARCIAL TP 💰 (50%)', pnl_partial, capital_now)

    except Exception as e:
        out(f'⚠️ Parcial SHORT {sym} falló: {e}')


def _audit_orphans(state):
    """
    Detecta activos spot con valor > $5 que no tienen posición registrada en el state.
    Si encuentra uno: intenta colocar un OCO de protección y lo agrega al state.
    Evita que un activo quede desprotegido por ciclos duplicados o errores de escritura.

    Fix #7: excluye falsos positivos:
    - Activos en cooldown (el par USDT está en cooldown_symbols) → solo monitoreo, no OCO
    - Activos en proceso de limpieza de polvo (dust_in_progress) → ignorar
    - Activos sin par USDT en futures (no tradeable como short) pero sí en spot
    """
    try:
        active_syms = {p['symbol'] for p in state.get('positions', []) if p['direction'] == 'long'}
        # Fix #7a: obtener cooldowns activos para excluirlos de la recuperación automática
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
            # Si hay cantidad bloqueada es porque hay una orden activa (OCO, limit) → no es huérfano
            if locked > 0:
                continue
            sym    = asset + 'USDT'
            price  = all_prices.get(sym, 0)
            if price == 0 or total * price < 5.0:
                continue
            if sym in active_syms:
                continue

            # Fix #7b: si el par está en cooldown, no es un huérfano accionable — solo alertar
            if sym in cooldown_syms:
                cd_info = state.get('cooldown_symbols', {})
                expiry  = cd_info.get(sym, 0) if isinstance(cd_info, dict) else 0
                rem_h   = max(0, (expiry - int(time.time())) / 3600) if expiry else 0
                out(f'ℹ️ {asset} en cooldown ({rem_h:.1f}h restantes), no se coloca OCO automático')
                continue

            # Fix #7c: si hay limpieza de polvo en progreso, ignorar activos pequeños
            # (pueden ser residuos de conversiones parciales que se limpiarán solas)
            if dust_in_progress and total * price < 15.0:
                continue

            # Activo huérfano detectado
            msg = f'⚠️ Activo huérfano detectado: {asset} ({total:.4f} = ${total*price:.2f})'
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
                    raise ValueError(f'precios inválidos: sl={sl} cur={cur} tp={tp} qty={qty}')

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
                utils.save_state(state)
                ok_msg = f'✅ {asset} recuperado: OCO colocado (SL=${sl:.4f} TP=${tp:.4f})'
                out(ok_msg)
                utils.send_alert(ok_msg)

            except Exception as e:
                out(f'❌ No se pudo proteger {asset}: {e}')
                utils.send_alert(f'🚨 {asset} huérfano sin OCO: {e}. Requiere intervención manual.')

    except Exception as e:
        out(f'⚠️ Auditoría falló: {e}')


def _maybe_clean_dust(state):
    """
    Convierte polvo a BNB de a un activo por ciclo (rate limit: 1/hora de Binance).
    La limpieza inicial arranca el lunes; luego sigue ciclo a ciclo hasta terminar.
    """
    import time as _time
    now     = int(_time.time())
    last    = state.get('last_dust_clean', 0)
    weekday = _time.gmtime(now).tm_wday

    # Arrancar limpieza si: es lunes Y pasó al menos 1 semana desde la última
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
        out(f'🧹 Polvo: {msg}')
        utils.send_alert(f'🧹 {msg}')
        state['last_dust_conversion'] = now
        state['dust_in_progress']     = True   # seguir el próximo ciclo
    elif 'Rate limit' in msg:
        pass  # silencio, reintenta sólo
    elif 'Sin polvo' in msg or 'insuficiente' in msg:
        # Terminó, limpiar flags
        state['last_dust_clean']      = now
        state['dust_in_progress']     = False
        if nueva_semana:
            out(f'🧹 Limpieza de polvo completada')
    else:
        # Ningún activo convertíble esta vuelta, seguir intentando
        state['last_dust_conversion'] = now


if __name__ == '__main__':
    main()
