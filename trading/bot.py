#!/usr/bin/env python3
"""
Orquestador principal del bot de trading.
Corre cada 10 min via cron. Gestiona longs (spot) y shorts (futures) simultáneamente.
"""
import sys, os, time, json
sys.path.insert(0, os.path.dirname(__file__))
import config, utils, market, longs, shorts

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
        state['daily_start_capital'] = utils.get_usdt_spot() + utils.get_total_futures()
        state['consec_sl']           = 0

    # ── Auditoría: activos huérfanos ─────────────────────────────────────────
    _audit_orphans(state)

    # ── Pausa global ─────────────────────────────────────────────────────────
    if state.get('status') == 'paused':
        out(f'⏸️ Bot pausado (límite diario). PnL hoy: {state.get("daily_pnl_usdt", 0):+.4f} USDT')
        utils.save_state(state)
        return

    # ── Contexto de mercado (una sola vez) ───────────────────────────────────
    btc_ctx = market.get_btc_context()
    trend   = btc_ctx['trend']
    chg4h   = btc_ctx['change_4h']
    force   = btc_ctx.get('force_mode')

    ctx_emoji = '🟢' if trend == 'bullish' else ('🔴' if trend == 'bearish' else '🟡')
    out(f'{ctx_emoji} Contexto BTC: {trend.upper()} | Precio: ${btc_ctx["btc_price"]:.0f} | 4h: {chg4h:+.2f}%')
    if force:
        out(f'⚡ Modo forzado: {force}')

    # ── 1. GESTIONAR posiciones activas ──────────────────────────────────────
    active_positions  = state.get('positions', [])
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
            _handle_close(state, pos, action, price_close, pnl)
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

    # ── 2a. LONGS ─────────────────────────────────────────────────────────────
    if long_count < max_longs and force != 'short_only':
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
        else:
            motivo = descarte_long.get('MERCADO', 'sin candidatos válidos')
            out(f'🔍 LONG: sin entrada ({motivo})')

    # ── 2b. SHORTS ────────────────────────────────────────────────────────────
    if short_count < max_shorts and force != 'long_only':
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
        else:
            motivo = descarte_short.get('MERCADO', 'sin candidatos válidos')
            out(f'🔍 SHORT: sin entrada ({motivo})')

    # ── Resumen ───────────────────────────────────────────────────────────────
    n_pos    = len(state['positions'])
    spot_bal  = utils.get_usdt_spot()
    # Total spot = USDT libre + valor de longs abiertos
    spot_in_positions = sum(
        p['entry_price'] * p['quantity']
        for p in positions_to_keep if p['direction'] == 'long'
    )
    spot_total = round(spot_bal + spot_in_positions, 2)
    spot_used  = round(spot_in_positions, 2)
    fut_total, fut_avail, fut_margin = utils.get_futures_summary()
    out(f'\n💼 Longs: {long_count}/{max_longs} | Shorts: {short_count}/{max_shorts} | Spot: ${spot_used:.2f}/${spot_total:.2f} | Futures: ${fut_margin:.2f}/${fut_total:.2f}')
    out(f'📊 PnL total: {state["total_pnl_usdt"]:+.4f} USDT | Hoy: {state["daily_pnl_usdt"]:+.4f} USDT')

    # ── Limpieza semanal de polvo ─────────────────────────────────────────────────────
    _maybe_clean_dust(state)

    utils.save_state(state)


# ── Helpers internos ──────────────────────────────────────────────────────────

def _handle_close(state, pos, action, price_close, pnl):
    """Procesa el cierre de una posición: actualiza estado, alerta, log."""
    sym       = pos['symbol']
    direction = pos['direction']

    state['trade_count']    = state.get('trade_count', 0) + 1
    state['total_pnl_usdt'] = round(state.get('total_pnl_usdt', 0) + pnl, 4)
    state['daily_pnl_usdt'] = round(state.get('daily_pnl_usdt', 0) + pnl, 4)
    capital_now = utils.get_usdt_spot() + utils.get_total_futures()

    label     = {'closed_tp': 'TP ✅', 'closed_sl': 'SL 🔴', 'closed_manual': 'SALIDA MANUAL ⏱️'}[action]
    dir_emoji = '📈' if direction == 'long' else '📉'
    msg = (
        f'{dir_emoji} {direction.upper()} {sym} cerrado: {label}\n'
        f'PnL: {pnl:+.4f} USDT | Acumulado: {state["total_pnl_usdt"]:+.4f} USDT'
    )
    out(msg)
    utils.send_alert(msg)
    utils.log_trade(state['trade_count'], sym, direction, label, pnl, capital_now)

    if action == 'closed_sl':
        state['consec_sl'] = state.get('consec_sl', 0) + 1
        if config.COOLDOWN_AFTER_SL:
            utils.add_cooldown(state, sym)
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

    try:
        # Cancelar OCO
        if oco_id:
            utils.spot_signed('DELETE', '/api/v3/orderList', {'orderListId': int(oco_id)})

        # Vender mitad
        utils.spot_signed('POST', '/api/v3/order', {
            'symbol': sym, 'side': 'SELL', 'type': 'MARKET', 'quantity': str(qty_half)
        })

        pnl_partial = (price - entry) * qty_half
        tick = utils.get_spot_filters(sym).get('tick_size', 0.0001)

        # Nuevo OCO con SL en breakeven (entrada) y mismo TP
        new_sl       = utils.round_tick(entry * 1.001, tick)   # breakeven + 0.1%
        new_sl_limit = utils.round_tick(new_sl * 0.999, tick)
        new_tp       = utils.round_tick(tp, tick)

        oco = utils.spot_signed('POST', '/api/v3/order/oco', {
            'symbol':               sym,
            'side':                 'SELL',
            'quantity':             str(qty_rest),
            'price':                str(new_tp),
            'stopPrice':            str(new_sl),
            'stopLimitPrice':       str(new_sl_limit),
            'stopLimitTimeInForce': 'GTC',
        })

        pos['quantity']            = qty_rest
        pos['sl']                  = new_sl
        pos['oco_order_list_id']   = str(oco.get('orderListId', ''))
        pos['oco_order_ids']       = [str(o['orderId']) for o in oco.get('orders', [])]
        pos['partial_taken']       = True

        msg = (
            f'💰 PARCIAL LONG {sym}: vendí 50% @ ${price:.4f}\n'
            f'PnL parcial: +${pnl_partial:.4f} | SL movido a breakeven ${new_sl:.4f}'
        )
        out(msg)
        utils.send_alert(msg)
        # Registrar PnL parcial en el state
        state['total_pnl_usdt'] = round(state.get('total_pnl_usdt', 0) + pnl_partial, 4)
        state['daily_pnl_usdt'] = round(state.get('daily_pnl_usdt', 0) + pnl_partial, 4)

    except Exception as e:
        out(f'⚠️ Parcial LONG {sym} falló: {e}')


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
        new_sl = utils.round_tick(entry * 0.999, tick)   # breakeven - 0.1% (por fees)
        new_tp = utils.round_tick(tp, tick)

        tp_order = utils.fut_signed('POST', '/fapi/v1/order', {
            'symbol': sym, 'side': 'BUY', 'type': 'LIMIT',
            'price': str(new_tp), 'quantity': str(qty_rest),
            'reduceOnly': 'true', 'timeInForce': 'GTC',
        })

        pos['quantity']      = qty_rest
        pos['sl']            = new_sl
        pos['tp_order_id']   = str(tp_order.get('orderId', ''))
        pos['partial_taken'] = True

        msg = (
            f'💰 PARCIAL SHORT {sym}: cerré 50% @ ${fill:.4f}\n'
            f'PnL parcial: +${pnl_partial:.4f} | SL movido a breakeven ${new_sl:.4f}'
        )
        out(msg)
        utils.send_alert(msg)
        # Registrar PnL parcial en el state
        state['total_pnl_usdt'] = round(state.get('total_pnl_usdt', 0) + pnl_partial, 4)
        state['daily_pnl_usdt'] = round(state.get('daily_pnl_usdt', 0) + pnl_partial, 4)

    except Exception as e:
        out(f'⚠️ Parcial SHORT {sym} falló: {e}')


def _audit_orphans(state):
    """
    Detecta activos spot con valor > $5 que no tienen posición registrada en el state.
    Si encuentra uno: intenta colocar un OCO de protección y lo agrega al state.
    Evita que un activo quede desprotegido por ciclos duplicados o errores de escritura.
    """
    try:
        active_syms = {p['symbol'] for p in state.get('positions', []) if p['direction'] == 'long'}

        # Precios en batch
        import urllib.request as _ur
        all_prices = {}
        try:
            r = _ur.urlopen(f'{config.SPOT_BASE}/api/v3/ticker/price', timeout=8)
            for p in json.loads(r.read()):
                all_prices[p['symbol']] = float(p['price'])
        except Exception:
            pass

        acc = utils.spot_signed('GET', '/api/v3/account')
        for b in acc['balances']:
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
