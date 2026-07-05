#!/usr/bin/env python3
"""
Módulo LONG — gestión de posiciones long en Spot.
Abre, monitorea, toma parcial, trailing stop, cierra.
"""
import sys, os, time, math
sys.path.insert(0, os.path.dirname(__file__))
import utils, config, capital_manager, decision_timeline, binance_client, residuals

BINANCE = binance_client.get_default_client()


def _asset_from_symbol(symbol):
    return str(symbol).replace('USDT', '')


def _spot_free_asset(symbol):
    return BINANCE.get_asset_spot(_asset_from_symbol(symbol))


def _adjust_spot_qty(symbol, requested_qty, price=None, filters=None):
    filters = filters or BINANCE.get_spot_filters(symbol)
    step = filters.get('step_size', 0.001)
    min_qty = filters.get('min_qty', 0.001)
    min_notional = filters.get('min_notional', 5.0)
    free_balance = _spot_free_asset(symbol)
    qty = utils.round_step(min(float(requested_qty or 0), free_balance), step)
    if qty < min_qty:
        return 0.0, free_balance
    if price is not None and qty * float(price) < min_notional:
        return 0.0, free_balance
    return qty, free_balance


def _build_oco_params(sym, qty, tp, sl, tick):
    sl_limit = utils.round_tick(sl * 0.999, tick)
    return {
        'symbol':               sym,
        'side':                 'SELL',
        'quantity':             str(qty),
        'price':                str(tp),
        'stopPrice':            str(sl),
        'stopLimitPrice':       str(sl_limit),
        'stopLimitTimeInForce': 'GTC',
    }


def _recovery_pending_position(sym, actual_price, qty, real_sl, real_tp, atr, reason):
    return {
        'id':                f'long_{sym}_{int(time.time())}_UNPROTECTED',
        'direction':         'long',
        'symbol':            sym,
        'entry_price':       actual_price,
        'quantity':          qty,
        'sl':                real_sl,
        'tp':                real_tp,
        'atr':               atr,
        'oco_order_list_id': '',
        'oco_order_ids':     [],
        'entry_time':        int(time.time()),
        'partial_taken':     False,
        'trail_peak':        actual_price,
        'recovery_pending':  True,
        'protection_warning': reason,
    }


def open_long(candidate, state, max_longs=None):
    """
    Abre una posición long en spot para el candidato dado.
    Retorna (posición dict, mensaje) o (None, error_msg).
    """
    sym    = candidate['symbol']
    sl     = candidate['sl']
    tp     = candidate['tp']
    try:
        decision_timeline.record_signal_evaluated(
            sym, 'LONG', 'LONG open flow started',
            details={'score': candidate.get('score'), 'reasons': candidate.get('reasons')},
        )
    except Exception:
        pass

    usdt = BINANCE.get_usdt_spot()
    if usdt < 5.0:
        return None, f'Capital spot insuficiente: ${usdt:.2f} USDT (mínimo $5)'
    # Total capital spot = libre + en posiciones abiertas
    spot_in_pos = sum(
        p['entry_price'] * p['quantity']
        for p in state.get('positions', []) if p['direction'] == 'long'
    )
    spot_total = usdt + spot_in_pos
    risk_pct = utils.get_spot_risk_pct(spot_total, state.get('consec_sl', 0))
    # Reducir riesgo si contexto macro es bajista
    if candidate.get('bearish_context'):
        risk_pct = min(risk_pct, config.SPOT_RISK_BEARISH)
    # Reducir riesgo si el token es volátil/riesgoso
    if candidate.get('risky'):
        risk_pct = min(risk_pct, config.RISKY_RISK_FACTOR)
    max_longs = max_longs if max_longs is not None else utils.get_max_long_positions(spot_total)
    ok_capacity, capacity_msg, _, _ = utils.validate_position_capacity(state, 'long', max_longs)
    if not ok_capacity:
        return None, capacity_msg
    capital_budget = utils.get_spot_capital_per_position(state, usdt)
    capital  = min(usdt * risk_pct, capital_budget)

    # Dry-run: simular sin ejecutar
    if config.DRY_RUN:
        price = BINANCE.get_spot_price(sym)
        try:
            ok, limit_msg, _ = capital_manager.validate_spot_order(
                state, spot_total, capital, max_longs
            )
        except Exception as e:
            return None, f'CAPITAL LIMIT ERROR SPOT: {e}'
        if not ok:
            return None, limit_msg
        atr_v = candidate['atr']
        real_sl = round(price - config.SL_ATR_MULT * atr_v, 8)
        real_tp = round(price + config.TP_ATR_MULT * atr_v, 8)
        pos = {
            'id': f'long_{sym}_{int(time.time())}_DRY',
            'direction': 'long', 'symbol': sym,
            'entry_price': price, 'quantity': round(capital / price, 4),
            'sl': real_sl, 'tp': real_tp, 'atr': atr_v,
            'oco_order_list_id': 'DRY', 'oco_order_ids': [],
            'entry_time': int(time.time()), 'partial_taken': False, 'trail_peak': price,
            'dry_run': True,
        }
        return pos, f'[DRY-RUN] LONG {sym} @ ${price:.4f} SL=${real_sl:.4f} TP=${real_tp:.4f}'

    # Filtros spot
    try:
        filters  = BINANCE.get_spot_filters(sym)
        step     = filters.get('step_size', 0.001)
        min_qty  = filters.get('min_qty', 0.001)
        min_not  = filters.get('min_notional', 5.0)
        tick     = filters.get('tick_size', 0.0001)
    except Exception as e:
        return None, f'Error filtros {sym}: {e}'

    price = BINANCE.get_spot_price(sym)
    qty   = utils.round_step(capital / price, step)

    if qty < min_qty:
        return None, f'Cantidad mínima no alcanzada: {qty} < {min_qty}'
    if qty * price < min_not:
        return None, f'Notional mínimo no alcanzado: ${qty * price:.2f} < ${min_not}'
    try:
        ok, limit_msg, _ = capital_manager.validate_spot_order(
            state, spot_total, qty * price, max_longs
        )
    except Exception as e:
        return None, f'CAPITAL LIMIT ERROR SPOT: {e}'
    if not ok:
        return None, limit_msg

    # Redondear SL/TP al tick
    sl = utils.round_tick(sl, tick)
    tp = utils.round_tick(tp, tick)

    # Compra MARKET (con backoff ante errores transitorios de API)
    buy = None
    last_err = None
    for _attempt in range(4):
        params = {
            'symbol':   sym,
            'side':     'BUY',
            'type':     'MARKET',
            'quantity': str(qty),
        }
        try:
            decision_timeline.record_order_event('order_sent', sym, 'LONG', f'BUY MARKET {sym}', details=params)
            buy = BINANCE.spot_signed('POST', '/api/v3/order', params)
            decision_timeline.record_order_event('order_opened', sym, 'LONG', f'LONG {sym} buy filled', details=buy)
            break  # éxito
        except Exception as e:
            if hasattr(e, 'code'):
                utils.log_binance_http_error('spot market buy', sym, 'BUY', 'MARKET', params, e)
            last_err = e
            if _attempt < 3:
                _delay = 10 * (2 ** _attempt)  # 10s, 20s, 40s
                import logging
                logging.warning(f'LONG {sym}: intento {_attempt+1} fallido ({e}), reintentando en {_delay}s')
                time.sleep(_delay)
    if buy is None:
        return None, f'Error al comprar {sym} tras 4 intentos: {last_err}'

    # Precio real de fill
    exec_qty = float(buy.get('executedQty', qty))
    cum_quote = float(buy.get('cummulativeQuoteQty', qty * price))
    actual_price = cum_quote / exec_qty if exec_qty else price
    qty_for_oco, real_asset_balance = _adjust_spot_qty(sym, exec_qty, actual_price, filters)
    real_sl = utils.round_tick(actual_price - config.SL_ATR_MULT * candidate['atr'], tick)
    real_tp = utils.round_tick(actual_price + config.TP_ATR_MULT * candidate['atr'], tick)
    sl_dist = (actual_price - real_sl) / actual_price * 100
    if sl_dist < config.SL_MIN_DIST_PCT:
        real_sl = utils.round_tick(actual_price * (1 - config.SL_MIN_DIST_PCT / 100), tick)
    if qty_for_oco <= 0:
        pos = _recovery_pending_position(
            sym, actual_price, 0.0, real_sl, real_tp, candidate['atr'],
            f'Balance real no vendible/protegible despues de compra (balance={real_asset_balance:.8f})'
        )
        return pos, (
            f'LONG {sym} comprado pero balance real insuficiente para OCO '
            f'(balance={real_asset_balance:.8f}). Revisión manual requerida.'
        )

    # OCO (SL + TP)
    oco_id   = ''
    oco_oids = []
    oco_err  = None
    for attempt in range(config.OCO_MAX_RETRIES):
        try:
            # SL debe estar bajo el precio, TP sobre el precio
            oco_params = _build_oco_params(sym, qty_for_oco, real_tp, real_sl, tick)
            oco = BINANCE.spot_signed('POST', '/api/v3/order/oco', oco_params)
            decision_timeline.record_protection_event('oco_created', sym, 'LONG', f'OCO protection OK for {sym}', details=oco_params)
            oco_id   = str(oco.get('orderListId', ''))
            oco_oids = [str(o['orderId']) for o in oco.get('orders', [])]
            break
        except Exception as e:
            oco_err = e
            if hasattr(e, 'code'):
                details = utils.log_binance_http_error('spot OCO create', sym, 'SELL', 'OCO', oco_params, e)
                oco_err = (
                    f'HTTP {details.get("status")} code={details.get("code")} msg={details.get("msg")}'
                    if details.get('code') is not None or details.get('msg') else str(e)
                )
                if details.get('code') == -2010:
                    qty_retry, real_asset_balance = _adjust_spot_qty(sym, qty_for_oco, actual_price, filters)
                    if qty_retry > 0:
                        qty_for_oco = qty_retry
                        try:
                            oco_params = _build_oco_params(sym, qty_for_oco, real_tp, real_sl, tick)
                            oco = BINANCE.spot_signed('POST', '/api/v3/order/oco', oco_params)
                            decision_timeline.record_protection_event('oco_retry_created', sym, 'LONG', f'OCO retry protection OK for {sym}', details=oco_params)
                            oco_id = str(oco.get('orderListId', ''))
                            oco_oids = [str(o['orderId']) for o in oco.get('orders', [])]
                            break
                        except Exception as retry_error:
                            oco_err = retry_error
                            if hasattr(retry_error, 'code'):
                                retry_details = utils.log_binance_http_error(
                                    'spot OCO create retry real balance', sym, 'SELL', 'OCO', oco_params, retry_error
                                )
                                oco_err = (
                                    f'HTTP {retry_details.get("status")} code={retry_details.get("code")} msg={retry_details.get("msg")}'
                                    if retry_details.get('code') is not None or retry_details.get('msg') else str(retry_error)
                                )
            time.sleep(2 ** attempt)

    # Si OCO falló, vender en mercado (emergencia)
    if not oco_id:
        sell_err = None
        try:
            sold_quote, _ = _market_sell(sym, qty_for_oco, price=actual_price, filters=filters)
            if sold_quote > 0:
                return None, f'OCO fallo ({oco_err}), posicion cerrada en emergencia'
            sell_err = 'balance real insuficiente para sell emergencia'
            raise RuntimeError(sell_err)
        except Exception as e2:
            sell_err = sell_err or e2
            sell_params = {
                'symbol':   sym,
                'side':     'SELL',
                'type':     'MARKET',
                'quantity': str(qty_for_oco),
            }
            if hasattr(e2, 'code'):
                details = utils.log_binance_http_error('spot emergency sell', sym, 'SELL', 'MARKET', sell_params, e2)
                sell_err = (
                    f'HTTP {details.get("status")} code={details.get("code")} msg={details.get("msg")}'
                    if details.get('code') is not None or details.get('msg') else str(e2)
                )
            qty_recovery, real_asset_balance = _adjust_spot_qty(sym, qty_for_oco, actual_price, filters)
            if qty_recovery <= 0:
                pos = _recovery_pending_position(
                    sym, actual_price, 0.0, real_sl, real_tp, candidate['atr'],
                    f'OCO inicial fallo ({oco_err}); no queda balance vendible (balance={real_asset_balance:.8f})'
                )
                decision_timeline.record_protection_event(
                    'recovery_pending', sym, 'LONG',
                    f'LONG {sym} unprotected: OCO failed and no sellable balance',
                    level='CRITICAL', details={'oco_error': str(oco_err), 'balance': real_asset_balance},
                    related_trade_id=pos.get('id'),
                )
                return pos, (
                    f'OCO fallo ({oco_err}) y no queda balance vendible '
                    f'(balance={real_asset_balance:.8f}); limpiando sin posicion local.'
                )
            qty_for_oco = qty_recovery
            pos = {
                'id':                f'long_{sym}_{int(time.time())}_UNPROTECTED',
                'direction':         'long',
                'symbol':            sym,
                'entry_price':       actual_price,
                'quantity':          qty_for_oco,
                'sl':                real_sl,
                'tp':                real_tp,
                'atr':               candidate['atr'],
                'oco_order_list_id': '',
                'oco_order_ids':     [],
                'entry_time':        int(time.time()),
                'partial_taken':     False,
                'trail_peak':        actual_price,
                'recovery_pending':  True,
                'protection_warning': f'OCO inicial fallo ({oco_err}); sell emergencia fallo ({sell_err})',
            }
            decision_timeline.record_protection_event(
                'recovery_pending', sym, 'LONG',
                f'LONG {sym} opened without OCO; recovery pending',
                level='CRITICAL', details={'oco_error': str(oco_err), 'sell_error': str(sell_err)},
                related_trade_id=pos.get('id'),
            )
            return pos, (
                f'⚠️ LONG {sym} abierto sin OCO inicial; sell emergencia fallo. '
                f'Recovery automatico intentara recolocar OCO con balance real. Motivo: {oco_err}'
            )

    pos = {
        'id':                f'long_{sym}_{int(time.time())}',
        'direction':         'long',
        'symbol':            sym,
        'entry_price':       actual_price,
        'quantity':          qty_for_oco,
        'sl':                real_sl,
        'tp':                real_tp,
        'atr':               candidate['atr'],
        'oco_order_list_id': oco_id,
        'oco_order_ids':     oco_oids,
        'entry_time':        int(time.time()),
        'partial_taken':     False,
        'trail_peak':        actual_price,
    }

    fee = qty_for_oco * actual_price * config.BNB_FEE_RATE
    pnl_tp = round((real_tp - actual_price) * qty_for_oco - fee * 2, 4)
    pnl_sl = round((real_sl - actual_price) * qty_for_oco - fee * 2, 4)

    msg = (
        f'📈 LONG abierto: {sym}\n'
        f'Entrada: ${actual_price:.4f} | Cant.: {qty_for_oco}\n'
        f'SL: ${real_sl:.4f} | TP: ${real_tp:.4f}\n'
        f'Si TP: +${pnl_tp:.4f} | Si SL: ${pnl_sl:.4f}'
    )
    return pos, msg


def manage_long(pos, state):
    """
    Gestiona una posición long activa. Verifica si el OCO se ejecutó,
    hace trailing stop, parcial, stale exit.
    Retorna (acción, mensaje):
      acción: 'hold' | 'closed_tp' | 'closed_sl' | 'closed_manual' | 'updated'
    """
    sym   = pos['symbol']
    entry = pos['entry_price']
    qty   = pos['quantity']
    oco_id = pos.get('oco_order_list_id', '')

    # Verificar si el OCO sigue activo
    if oco_id:
        try:
            oco_status = BINANCE.spot_signed('GET', '/api/v3/orderList', {'orderListId': int(oco_id)})
            list_status = oco_status.get('listOrderStatus', '')

            if list_status in ('ALL_DONE', 'FILLED'):
                # Determinar si fue TP o SL
                for o in oco_status.get('orders', []):
                    order_detail = BINANCE.spot_signed('GET', '/api/v3/order', {
                        'symbol':  sym,
                        'orderId': o['orderId']
                    })
                    if order_detail.get('status') == 'FILLED':
                        exec_qty  = float(order_detail.get('executedQty', qty))
                        cum_quote = float(order_detail.get('cummulativeQuoteQty', 0))
                        fill_price = cum_quote / exec_qty if exec_qty else float(order_detail.get('price', 0))
                        order_type = order_detail.get('type', '')
                        pnl = (fill_price - entry) * qty * (1 - config.BNB_FEE_RATE * 2)
                        if 'STOP' in order_type or fill_price < entry * 0.999:
                            return 'closed_sl', fill_price, pnl
                        else:
                            return 'closed_tp', fill_price, pnl

            elif list_status == 'EXECUTING':
                pass  # normal, sigue activo
        except Exception as e:
            pass  # si falla la verificación, continuar

    # Si no hay OCO válido, intentar recolocar
    if not oco_id:
        return _recolocar_oco(pos, state)

    price_now = BINANCE.get_spot_price(sym)

    # Stale exit por tiempo máximo (12h) — aunque esté en profit
    elapsed_h = (time.time() - pos.get('entry_time', time.time())) / 3600
    if elapsed_h > config.STALE_MAX_HOURS:
        _cancel_oco(sym, oco_id)
        cum_quote, fill_price = _market_sell(sym, qty)
        pnl = (fill_price - entry) * qty * (1 - config.BNB_FEE_RATE * 2) if fill_price else 0.0
        return 'closed_manual', fill_price or price_now, pnl
    
    # Stale exit por poco movimiento (<0.5% en 5h)
    price_pct  = abs(price_now - entry) / entry * 100
    if elapsed_h > config.STALE_HOURS and price_pct < config.STALE_RANGE_PCT:
        _cancel_oco(sym, oco_id)
        cum_quote, fill_price = _market_sell(sym, qty)
        pnl = (fill_price - entry) * qty * (1 - config.BNB_FEE_RATE * 2) if fill_price else 0.0
        return 'closed_manual', fill_price or price_now, pnl

    # Trailing stop
    trail_peak = pos.get('trail_peak', entry)
    if price_now > trail_peak * (1 + config.TRAIL_STEP_PCT / 100):
        new_peak = price_now
        atr_v    = pos.get('atr', (price_now - pos['sl']))
        new_sl   = utils.round_tick(price_now - config.SL_ATR_MULT * atr_v, 0.0001)
        if new_sl > pos['sl']:
            # Cancelar OCO viejo y colocar nuevo
            _cancel_oco(sym, oco_id)
            # Nuevo OCO con SL subido
            tick = 0.0001
            try:
                filters = BINANCE.get_spot_filters(sym)
                tick    = filters.get('tick_size', 0.0001)
            except Exception:
                pass
            new_sl = utils.round_tick(new_sl, tick)
            new_tp = utils.round_tick(pos['tp'], tick)
            try:
                new_sl_limit = utils.round_tick(new_sl * 0.999, tick)
                oco = BINANCE.spot_signed('POST', '/api/v3/order/oco', {
                    'symbol':               sym,
                    'side':                 'SELL',
                    'quantity':             str(qty),
                    'price':                str(new_tp),
                    'stopPrice':            str(new_sl),
                    'stopLimitPrice':       str(new_sl_limit),
                    'stopLimitTimeInForce': 'GTC',
                })
                pos['oco_order_list_id'] = str(oco.get('orderListId', ''))
                pos['oco_order_ids']     = [str(o['orderId']) for o in oco.get('orders', [])]
                pos['sl']        = new_sl
                pos['trail_peak'] = new_peak
                return 'updated', price_now, 0
            except Exception as e:
                pass

    return 'hold', price_now, 0


def _cancel_oco(symbol, oco_id):
    try:
        BINANCE.spot_signed('DELETE', '/api/v3/orderList', {
            'symbol':      symbol,
            'orderListId': int(oco_id),
        })
    except Exception:
        pass


def _market_sell(symbol, qty, price=None, filters=None):
    if price is None:
        try:
            price = BINANCE.get_spot_price(symbol)
        except Exception:
            price = None
    qty_to_sell, _ = _adjust_spot_qty(symbol, qty, price, filters)
    if qty_to_sell <= 0:
        return 0.0, 0.0
    params = {
        'symbol':   symbol,
        'side':     'SELL',
        'type':     'MARKET',
        'quantity': str(qty_to_sell),
    }
    try:
        order = BINANCE.spot_signed('POST', '/api/v3/order', params)
        # Calcular precio real de fill desde los fills
        fills = order.get('fills', [])
        if fills:
            total_quote = sum(float(f['price']) * float(f['qty']) for f in fills)
            total_qty   = sum(float(f['qty']) for f in fills)
            return float(order.get('cummulativeQuoteQty', 0)), total_quote / total_qty if total_qty else 0
        # Fallback: cummulativeQuoteQty / executedQty
        exec_qty   = float(order.get('executedQty', qty_to_sell))
        cum_quote  = float(order.get('cummulativeQuoteQty', 0))
        fill_price = cum_quote / exec_qty if exec_qty else 0
        return cum_quote, fill_price
    except Exception as e:
        if hasattr(e, 'code'):
            utils.log_binance_http_error('spot market sell', symbol, 'SELL', 'MARKET', params, e)
        return 0.0, 0.0


def _recolocar_oco(pos, state):
    """Intenta recolocar OCO si está vacío."""
    sym   = pos['symbol']
    sl    = pos['sl']
    tp    = pos['tp']
    qty   = pos['quantity']
    price_now = BINANCE.get_spot_price(sym)

    if price_now <= sl:
        # Ya tocó el SL, cerrar en mercado
        _market_sell(sym, qty, price=price_now)
        pnl = (price_now - pos['entry_price']) * qty
        return 'closed_sl', price_now, pnl

    try:
        filters = BINANCE.get_spot_filters(sym)
        tick = filters.get('tick_size', 0.0001)
        qty, real_asset_balance = _adjust_spot_qty(sym, qty, price_now, filters)
        if qty <= 0:
            if residuals.handle_unprotectable_spot_residual(
                sym,
                _asset_from_symbol(sym),
                real_asset_balance,
                price_now,
                filters,
            ):
                pos['quantity'] = real_asset_balance
                pos['recovery_pending'] = True
                return 'hold', price_now, 0
            pos['quantity'] = 0.0
            pos['recovery_pending'] = False
            pos['already_closed'] = True
            return 'closed_manual', price_now, 0
        sl_r = utils.round_tick(sl, tick)
        tp_r = utils.round_tick(tp, tick)
        sl_limit_r = utils.round_tick(sl_r * 0.999, tick)
        if residuals.handle_unprotectable_spot_residual(
            sym,
            _asset_from_symbol(sym),
            qty,
            price_now,
            filters,
        ):
            pos['quantity'] = qty
            pos['recovery_pending'] = True
            return 'hold', price_now, 0
        oco_params = {
            'symbol':               sym,
            'side':                 'SELL',
            'quantity':             str(qty),
            'price':                str(tp_r),
            'stopPrice':            str(sl_r),
            'stopLimitPrice':       str(sl_limit_r),
            'stopLimitTimeInForce': 'GTC',
        }
        oco = BINANCE.spot_signed('POST', '/api/v3/order/oco', oco_params)
        pos['oco_order_list_id'] = str(oco.get('orderListId', ''))
        pos['oco_order_ids']     = [str(o['orderId']) for o in oco.get('orders', [])]
        pos['quantity']          = qty
        pos['recovery_pending']  = False
        pos.pop('protection_warning', None)
        decision_timeline.record_protection_event(
            'recovery_success', sym, 'LONG', f'OCO recovery successful for {sym}',
            details=oco_params, related_trade_id=pos.get('id'),
        )
        utils.send_alert(f'⚠️ OCO inicial falló para {sym}, recuperación automática exitosa. Protección restablecida.')
        return 'updated', price_now, 0
    except Exception as e:
        reason = str(e)
        if hasattr(e, 'code'):
            details = utils.log_binance_http_error('spot OCO recovery', sym, 'SELL', 'OCO', locals().get('oco_params', {}), e)
            reason = details.get('msg') or details.get('raw_body') or reason
        utils.send_alert(
            f'🚨 {sym} LONG sigue SIN OCO. Cantidad={qty}. Motivo Binance={reason}. '
            f'Acción requerida: revisar/cerrar o proteger manualmente.'
        )
        decision_timeline.record_protection_event(
            'recovery_failed', sym, 'LONG', f'{sym} LONG remains without OCO',
            level='CRITICAL', details={'reason': reason}, related_trade_id=pos.get('id'),
        )
        return 'hold', price_now, 0
