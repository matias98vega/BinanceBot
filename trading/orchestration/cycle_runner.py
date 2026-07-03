#!/usr/bin/env python3
"""Main cycle orchestration extracted from bot.py without changing behavior."""

import time

import bot_state
import capital_manager
import config
import decision_timeline
import longs
import market
import rebalance
import shorts
import utils


class CycleRunner:
    def __init__(
        self,
        *,
        out_fn,
        analytics,
        binance,
        safe_log_open_fn,
        safe_log_close_fn,
        safe_log_decision_snapshot_fn,
        safe_persist_bot_state_fn,
        audit_orphans_fn,
        maybe_clean_dust_fn,
        check_partial_long_fn,
        check_partial_short_fn,
        handle_close_fn,
    ):
        self.out = out_fn
        self.analytics = analytics
        self.binance = binance
        self.safe_log_open = safe_log_open_fn
        self.safe_log_close = safe_log_close_fn
        self.safe_log_decision_snapshot = safe_log_decision_snapshot_fn
        self.safe_persist_bot_state = safe_persist_bot_state_fn
        self.audit_orphans = audit_orphans_fn
        self.maybe_clean_dust = maybe_clean_dust_fn
        self.check_partial_long = check_partial_long_fn
        self.check_partial_short = check_partial_short_fn
        self.handle_close = handle_close_fn

    def run(self):
        cycle_id = f'cycle_{int(time.time())}'
        state = utils.load_state()
        try:
            decision_timeline.record_cycle_start(cycle_id=cycle_id, details={'positions': len(state.get('positions', []))})
        except Exception:
            pass

        # â”€â”€ Migrar cooldown_symbols de lista a dict si es necesario â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if isinstance(state.get('cooldown_symbols'), list):
            state['cooldown_symbols'] = {s: 0 for s in state['cooldown_symbols']}

        # â”€â”€ Reset diario â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        today = time.strftime('%Y-%m-%d', time.gmtime())
        if state.get('pnl_date') != today:
            if state.get('status') == 'paused':
                state['status'] = 'active'
                self.out('âœ… Nuevo dÃ­a â€” bot reactivado.')
            state['pnl_date']            = today
            state['daily_pnl_usdt']      = 0.0
            spot_free = self.binance.get_usdt_spot()
            spot_in_pos = sum(
                p['entry_price'] * p['quantity']
                for p in state.get('positions', []) if p['direction'] == 'long'
            )
            state['daily_start_capital'] = spot_free + spot_in_pos + self.binance.get_total_futures()
            state['consec_sl']           = 0

        # â”€â”€ AuditorÃ­a: activos huÃ©rfanos â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        self.audit_orphans(state)

        # â”€â”€ RevisiÃ³n blacklist dinÃ¡mica (cada 6h) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        last_bl_review = state.get('last_bl_review', 0)
        if time.time() - last_bl_review > 21600:  # 6 horas
            rehabilitated = market.review_dynamic_blacklist()
            if rehabilitated:
                self.out(f'âœ… Rehabilitados desde blacklist: {", ".join(rehabilitated)}')
            state['last_bl_review'] = int(time.time())

        # â”€â”€ Pausa global â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if state.get('status') == 'paused':
            self.out(f'â¸ï¸ Bot pausado (lÃ­mite diario). PnL hoy: {state.get("daily_pnl_usdt", 0):+.4f} USDT')
            self.safe_persist_bot_state(state, system_health='WARNING')
            utils.save_state(state)
            return

        # â”€â”€ Circuit breaker: pausa 24h si â‰¥4 SLs consecutivos â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if state.get('consec_sl', 0) >= 4:
            state['status'] = 'paused'
            state['pause_until'] = int(time.time()) + 86400  # 24h
            self.out('â›” Circuit breaker: 4 SLs consecutivos â†’ bot pausado por 24h')
            utils.send_alert('â›” Bot pausado por circuit breaker: 4 SLs consecutivos')
            try:
                self.analytics.log_event(
                    'CIRCUIT_BREAKER',
                    consec_sl=state.get('consec_sl', 0),
                    pause_until=state.get('pause_until'),
                    status=state.get('status'),
                )
            except Exception:
                pass
            self.safe_persist_bot_state(state, system_health='WARNING')
            utils.save_state(state)
            return

        # â”€â”€ Verificar si pausa por circuit breaker ya expirÃ³ â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        pause_until = state.get('pause_until', 0)
        if pause_until > 0 and int(time.time()) < pause_until:
            remaining_h = (pause_until - int(time.time())) / 3600
            self.out(f'â¸ï¸ Bot pausado (circuit breaker). Restan {remaining_h:.1f}h')
            self.safe_persist_bot_state(state, system_health='WARNING')
            utils.save_state(state)
            return
        elif pause_until > 0 and int(time.time()) >= pause_until:
            # Pausa expirÃ³, reactivar
            state['status'] = 'active'
            state['pause_until'] = 0
            state['consec_sl'] = 0
            self.out('âœ… Circuit breaker expirado â†’ bot reactivado')
            utils.save_state(state)

        # â”€â”€ MÃ¡ximo de posiciones abiertas simultÃ¡neas â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        MAX_OPEN_POSITIONS = 3  # con capital ~$50, no diversificar en exceso
        active_positions = state.get('positions', [])
        if len(active_positions) >= MAX_OPEN_POSITIONS:
            self.out(f'â¸ï¸ MÃ¡ximo de posiciones abiertas ({MAX_OPEN_POSITIONS}). Esperando cierres.')
            # No retornar â€” igual gestionar posiciones existentes

        # â”€â”€ Contexto de mercado (una sola vez) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        btc_ctx = market.get_btc_context()
        trend   = btc_ctx['trend']
        chg4h   = btc_ctx['change_4h']
        force   = btc_ctx.get('force_mode')

        ctx_emoji = 'ðŸŸ¢' if trend == 'bullish' else ('ðŸ”´' if trend == 'bearish' else 'ðŸŸ¡')
        self.out(f'{ctx_emoji} Contexto BTC: {trend.upper()} | Precio: ${btc_ctx["btc_price"]:.0f} | 4h: {chg4h:+.2f}%')
        try:
            decision_timeline.record_event(
                'market_context',
                f'BTC {trend} 4h {chg4h:+.2f}%',
                category='MARKET',
                cycle_id=cycle_id,
                details=btc_ctx,
            )
        except Exception:
            pass
        if force:
            self.out(f'âš¡ Modo forzado: {force}')

        # â”€â”€ Rebalanceo de capital segÃºn contexto â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        rb_ok, rb_msg = rebalance.rebalance(state, btc_ctx)
        if rb_ok:
            self.out(rb_msg)
            utils.send_alert(utils.format_rebalance_alert(rb_msg))

        # â”€â”€ 1. GESTIONAR posiciones activas â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # â”€â”€ 1a. Cierre preventivo por momentum extremo de BTC â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        close_shorts, close_longs, close_reason = market.check_btc_momentum_close(btc_ctx)
        if close_shorts or close_longs:
            self.out(f'ðŸš¨ {close_reason}')
            utils.send_alert(close_reason)
            
            # Cerrar posiciones afectadas
            for pos in active_positions[:]:
                direction = pos['direction']
                sym = pos['symbol']
                
                should_close = (direction == 'short' and close_shorts) or (direction == 'long' and close_longs)
                if should_close:
                    # Cerrar al mercado
                    if direction == 'short':
                        price_now = self.binance.get_fut_price(sym)
                        pnl = (pos['entry_price'] - price_now) * pos['quantity']
                        # Cancelar TP si existe
                        if pos.get('tp_order_id'):
                            try:
                                self.binance.fut_signed('DELETE', '/fapi/v1/order', {'symbol': sym, 'orderId': int(pos['tp_order_id'])})
                            except: pass
                    else:
                        price_now = self.binance.get_spot_price(sym)
                        pnl = (price_now - pos['entry_price']) * pos['quantity']
                        # Cancelar OCO si existe
                        if pos.get('oco_id'):
                            try:
                                self.binance.spot_signed('DELETE', '/api/v3/orderList', {'symbol': sym, 'orderListId': int(pos['oco_id'])})
                            except: pass
                    
                    # Remover de posiciones
                    self.out(f'  ðŸ”´ {sym} {direction}: cierre preventivo PnL={pnl:+.2f}')
                    self.safe_log_close(pos, price_now, 'PREVENTIVE_BTC_MOMENTUM', pnl)
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
                        self.out(f'Log trade write failed: {e}')
            
            # Recargar lista despuÃ©s de cierres
            active_positions = state.get('positions', [])

        # â”€â”€ 1b. GestiÃ³n normal de posiciones restantes â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        positions_to_keep = []

        for pos in active_positions:
            direction = pos['direction']
            sym       = pos['symbol']

            if direction == 'long':
                # Chequear take profit parcial antes de la gestiÃ³n normal
                self.check_partial_long(pos, state)
                action, price_close, pnl = longs.manage_long(pos, state)
            else:
                self.check_partial_short(pos, state)
                action, price_close, pnl = shorts.manage_short(pos, state)

            if action in ('closed_tp', 'closed_sl', 'closed_manual'):
                self.handle_close(state, pos, action, price_close, pnl, btc_ctx)
            elif action == 'updated':
                self.out(f'ðŸ”„ {direction.upper()} {sym} actualizado (trailing stop)')
                utils.send_alert(f'⚠️ {direction.upper()} {sym} actualizado: protección/trailing restablecido.')
                positions_to_keep.append(pos)
            else:
                # hold â€” mostrar estado
                if direction == 'short':
                    upnl = (pos['entry_price'] - price_close) * pos['quantity']
                    sl_dist = (pos['sl'] - price_close) / price_close * 100
                    self.out(f'  ðŸ“‰ {sym}: ${price_close:.4f} | uPnL: {upnl:+.4f} | SL dist: {sl_dist:.2f}%')
                else:
                    upnl = (price_close - pos['entry_price']) * pos['quantity']
                    self.out(f'  ðŸ“ˆ {sym}: ${price_close:.4f} | uPnL: {upnl:+.4f}')
                positions_to_keep.append(pos)

        state['positions'] = positions_to_keep

        if state.get('status') == 'paused':
            self.safe_persist_bot_state(state, btc_ctx=btc_ctx, system_health='WARNING')
            utils.save_state(state)
            return

        # â”€â”€ 2. EVALUAR nuevas entradas â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        active_symbols = {p['symbol'] for p in positions_to_keep}
        cooldowns      = utils.get_active_cooldowns(state)
        excluded       = active_symbols | cooldowns
        long_count     = sum(1 for p in positions_to_keep if p['direction'] == 'long')
        short_count    = sum(1 for p in positions_to_keep if p['direction'] == 'short')
        spot_free      = self.binance.get_usdt_spot()
        spot_in_positions = sum(
            p['entry_price'] * p['quantity']
            for p in positions_to_keep if p['direction'] == 'long'
        )
        spot_total_capital = spot_free + spot_in_positions
        fut_free       = self.binance.get_usdt_futures()
        max_longs      = utils.get_max_long_positions(spot_total_capital)
        max_shorts     = utils.get_max_short_positions(fut_free)

        if long_count >= max_longs:
            self.out(f'CAPACITY LIMIT REJECT: longs {long_count}/{max_longs}')
            try:
                decision_timeline.record_event(
                    'capacity_reject',
                    f'Long entries blocked: {long_count}/{max_longs}',
                    level='WARNING',
                    category='RISK',
                    cycle_id=cycle_id,
                    details={'direction': 'long', 'count': long_count, 'max': max_longs},
                )
            except Exception:
                pass
            if long_count > max_longs:
                self.out(f'Sobrecapacidad actual: Longs {long_count}/{max_longs}. No se abrirán nuevos longs hasta normalizar.')
        if short_count >= max_shorts:
            self.out(f'CAPACITY LIMIT REJECT: shorts {short_count}/{max_shorts}')
            try:
                decision_timeline.record_event(
                    'capacity_reject',
                    f'Short entries blocked: {short_count}/{max_shorts}',
                    level='WARNING',
                    category='RISK',
                    cycle_id=cycle_id,
                    details={'direction': 'short', 'count': short_count, 'max': max_shorts},
                )
            except Exception:
                pass
            if short_count > max_shorts:
                self.out(f'Sobrecapacidad actual: Shorts {short_count}/{max_shorts}. No se abrirán nuevos shorts hasta normalizar.')

        # Mostrar cooldowns activos si hay
        if cooldowns:
            cd_info = state.get('cooldown_symbols', {})
            now = int(time.time())
            cd_strs = []
            for sym in cooldowns:
                expiry = cd_info.get(sym, 0) if isinstance(cd_info, dict) else 0
                rem_h  = max(0, (expiry - now) / 3600) if expiry else 0
                cd_strs.append(f'{sym}({rem_h:.1f}h)')
            self.out(f'â³ Cooldown: {", ".join(cd_strs)}')

        # â”€â”€ Pausa post-SL: saltar entradas por 2 ciclos â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        skip_cycles = state.get('skip_next_cycles', 0)
        if skip_cycles > 0:
            state['skip_next_cycles'] = skip_cycles - 1
            self.out(f'â¸ï¸ Pausa post-SL: saltando ciclo de entradas ({skip_cycles} restantes)')
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
                self.out(f'ðŸ” Candidato LONG: {best_long["symbol"]} score={best_long["score"]} RSI={best_long["rsi"]:.0f} reasons={best_long["reasons"]}')
                try:
                    decision_timeline.record_signal_evaluated(
                        best_long.get('symbol'), 'LONG', 'LONG signal accepted for open attempt',
                        cycle_id=cycle_id, details=best_long,
                    )
                except Exception:
                    pass
                pos, msg = longs.open_long(best_long, state, max_longs=max_longs)
                if pos:
                    state['positions'].append(pos)
                    self.safe_log_open(pos, best_long, btc_ctx, spot_total_capital)
                    self.out(msg)
                    if pos.get('protection_warning'):
                        utils.send_alert(f'⚠️ {pos["symbol"]}: {pos["protection_warning"]}. Recovery automatico pendiente.')
                    utils.send_alert(utils.format_trade_open_alert(pos, best_long, btc_ctx.get('trend')))
                else:
                    self.out(f'âš ï¸ LONG no abierto: {msg}')
                    utils.send_alert(f'âš ï¸ FALLÃ“ apertura LONG {best_long["symbol"]}: {msg}')
                    # Log detallado para debugging
                    import logging
                    logging.error(f'LONG fallido {best_long["symbol"]}: {msg}')
            else:
                motivo = descarte_long.get('MERCADO', 'sin candidatos vÃ¡lidos')
                try:
                    decision_timeline.record_signal_rejected('LONG_SCAN', 'LONG', motivo, cycle_id=cycle_id, details=descarte_long)
                except Exception:
                    pass
                # No mostrar en consola si es por modo direccional (ya estÃ¡ en config)
                if 'modo direccional' not in motivo:
                    self.out(f'ðŸ” LONG: sin entrada ({motivo})')

        # â”€â”€ 2b. SHORTS â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if short_count < max_shorts and force != 'long_only' and not skip_new_entries:
            excl_short = {p['symbol'] for p in state['positions']} | cooldowns
            best_short, descarte_short = market.scan_shorts(btc_ctx, excluded_symbols=excl_short)
            utils.log_analysis('short', best_short, descarte_short)

            if best_short:
                self.out(f'ðŸ” Candidato SHORT: {best_short["symbol"]} score={best_short["score"]} RSI={best_short["rsi"]:.0f} reasons={best_short["reasons"]}')
                try:
                    decision_timeline.record_signal_evaluated(
                        best_short.get('symbol'), 'SHORT', 'SHORT signal accepted for open attempt',
                        cycle_id=cycle_id, details=best_short,
                    )
                except Exception:
                    pass
                pos, msg = shorts.open_short(best_short, state, max_shorts=max_shorts)
                if pos:
                    state['positions'].append(pos)
                    self.safe_log_open(pos, best_short, btc_ctx, fut_free)
                    self.out(msg)
                    utils.send_alert(utils.format_trade_open_alert(pos, best_short, btc_ctx.get('trend')))
                else:
                    self.out(f'âš ï¸ SHORT no abierto: {msg}')
                    utils.send_alert(f'âš ï¸ FALLÃ“ apertura SHORT {best_short["symbol"]}: {msg}')
                    # Log detallado para debugging
                    import logging
                    logging.error(f'SHORT fallido {best_short["symbol"]}: {msg}')
            else:
                motivo = descarte_short.get('MERCADO', 'sin candidatos vÃ¡lidos')
                try:
                    decision_timeline.record_signal_rejected('SHORT_SCAN', 'SHORT', motivo, cycle_id=cycle_id, details=descarte_short)
                except Exception:
                    pass
                # No mostrar en consola si es por modo direccional (ya estÃ¡ en config)
                if 'modo direccional' not in motivo:
                    self.out(f'ðŸ” SHORT: sin entrada ({motivo})')

        self.safe_log_decision_snapshot(btc_ctx, spot_total_capital, spot_free, fut_free)

        # â”€â”€ Resumen â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # Recalcular contadores reales (pueden haber cambiado si se abrio nueva posicion)
        long_count_final  = sum(1 for p in state['positions'] if p['direction'] == 'long')
        short_count_final = sum(1 for p in state['positions'] if p['direction'] == 'short')
        spot_bal  = self.binance.get_usdt_spot()
        # Total spot = USDT libre + valor de longs abiertos
        spot_in_positions = sum(
            p['entry_price'] * p['quantity']
            for p in state['positions'] if p['direction'] == 'long'
        )
        spot_total = round(spot_bal + spot_in_positions, 2)
        spot_used  = round(spot_in_positions, 2)
        fut_total, fut_avail, fut_margin = self.binance.get_futures_summary()
        futures_observability = {
            'futures_available_balance': fut_avail,
            'futures_position_margin': fut_margin,
        }
        try:
            futures_account = self.binance.futures_account()
            try:
                position_risk = self.binance.futures_position_risk()
            except Exception:
                position_risk = None
            futures_observability.update(bot_state.futures_observability_from_account(futures_account, position_risk))
        except Exception:
            pass
        # Valor nocional de posiciones short activas
        short_notional = sum(
            p['entry_price'] * p['quantity'] / p.get('leverage', config.FUTURES_LEVERAGE)
            for p in state['positions'] if p['direction'] == 'short'
        )
        try:
            short_count_observed = max(short_count_final, int(futures_observability.get('futures_open_positions_count')))
        except (TypeError, ValueError):
            short_count_observed = short_count_final
        futures_used_observed = futures_observability.get('futures_position_margin')
        if futures_used_observed is None:
            futures_used_observed = short_notional
        try:
            cap = capital_manager.snapshot(spot_total, fut_total)
            max_position_label = (
                f'{cap["max_position_percent"]:.2f}%'
                if cap.get('max_position_percent') is not None else 'off'
            )
            spot_max_op = capital_manager.max_margin_per_position(
                cap['spot_usable'], max_longs, cap['max_exposure_percent']
            )
            futures_max_op = capital_manager.max_margin_per_position(
                cap['futures_usable'], max_shorts, cap['max_exposure_percent']
            )
            self.out(
                f'Capital limits: Spot real ${cap["spot_real"]:.2f} usable ${cap["spot_usable"]:.2f} | '
                f'Futures real ${cap["futures_real"]:.2f} usable ${cap["futures_usable"]:.2f} | '
                f'Max op spot ${spot_max_op:.2f} futures ${futures_max_op:.2f} | '
                f'Max pos guardrail {max_position_label} | Max exposure {cap["max_exposure_percent"]:.2f}%'
            )
        except Exception as e:
            self.out(f'Capital limits: WARNING ({e})')
        bot_state_payload = None
        max_longs_console = max_longs
        max_shorts_console = max_shorts
        try:
            bot_state_payload = bot_state.build_bot_state(
                state=state,
                btc_ctx=btc_ctx,
                spot_real=spot_total,
                futures_real=fut_total,
                futures_observability=futures_observability,
                max_longs=max_longs,
                max_shorts=max_shorts,
                system_health='OK',
                bot_status='ONLINE',
            )
            position_state = bot_state_payload.get('positions') if isinstance(bot_state_payload.get('positions'), dict) else {}
            long_state = position_state.get('long') if isinstance(position_state.get('long'), dict) else {}
            short_state = position_state.get('short') if isinstance(position_state.get('short'), dict) else {}
            max_longs_console = long_state.get('max', max_longs)
            max_shorts_console = short_state.get('max', max_shorts)
        except Exception as e:
            self.out(f'BotState build warning: {e}')
        self.out(f'\nðŸ’¼ Longs: {long_count_final}/{max_longs_console} | Shorts: {short_count_final}/{max_shorts_console} | Spot: ${spot_used:.2f}/${spot_total:.2f} | Futures: ${short_notional:.2f}/${fut_total:.2f}')
        self.out(f'ðŸ“Š PnL total: {state["total_pnl_usdt"]:+.4f} USDT | Hoy: {state["daily_pnl_usdt"]:+.4f} USDT')
        try:
            decision_timeline.record_cycle_end(
                cycle_id=cycle_id,
                message=f'Cycle summary: longs {long_count_final}/{max_longs_console}, shorts {short_count_final}/{max_shorts_console}',
                details={
                    'longs': long_count_final,
                    'shorts': short_count_observed,
                    'max_longs': max_longs_console,
                    'max_shorts': max_shorts_console,
                    'spot_used': spot_used,
                    'spot_total': spot_total,
                    'futures_used': futures_used_observed,
                    'futures_total': fut_total,
                    'futures_available_balance': fut_avail,
                    'futures_position_margin': futures_observability.get('futures_position_margin'),
                    'pnl_total': state.get('total_pnl_usdt'),
                    'pnl_today': state.get('daily_pnl_usdt'),
                },
            )
        except Exception:
            pass
        if bot_state_payload is not None:
            try:
                bot_state.persist_bot_state(bot_state_payload)
            except Exception as e:
                self.out(f'BotState write warning: {e}')
        else:
            self.safe_persist_bot_state(
                state,
                btc_ctx=btc_ctx,
                spot_real=spot_total,
                futures_real=fut_total,
                max_longs=max_longs,
                max_shorts=max_shorts,
                system_health='OK',
            )

        # â”€â”€ Limpieza semanal de polvo â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        self.maybe_clean_dust(state)

        utils.save_state(state)


        # â”€â”€ Helpers internos â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
