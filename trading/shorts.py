#!/usr/bin/env python3
"""
Módulo SHORT — gestión de posiciones short en Futures (fapi).
SL: STOP_MARKET nativo en el exchange (si NATIVE_SL_ENABLED=True) + guardian software como fallback.
TP: orden LIMIT + reduceOnly=true.
"""
import sys, os, time, math
import urllib.error as _ue
sys.path.insert(0, os.path.dirname(__file__))
import utils, config, capital_manager


def _place_stop_market(symbol, side, stop_price, quantity, reduce_only=True):
    """
    Coloca una orden STOP_MARKET en Binance Futures.
    Si Binance devuelve -4120 (endpoint no soportado para esta cuenta/par),
    retorna None silenciosamente — el guardian software cubre como fallback.
    El endpoint Algo (/fapi/v1/order/algo) no está disponible en esta cuenta.
    """
    import json as _json, urllib.error as _ue
    params = {
        'symbol':     symbol,
        'side':       side,
        'type':       'STOP_MARKET',
        'stopPrice':  str(stop_price),
        'quantity':   str(quantity),
        'reduceOnly': 'true' if reduce_only else 'false',
    }
    try:
        return utils.fut_signed('POST', '/fapi/v1/order', params)
    except _ue.HTTPError as e:
        body = ''
        try:
            body = e.read().decode('utf-8')
        except Exception:
            pass
        code = -1
        try:
            code = _json.loads(body).get('code', -1)
        except Exception:
            pass
        if code == -4120:
            # SL nativo no soportado en esta cuenta para este par
            # Guardian software cubre como fallback — no es un error crítico
            return None
        # Otro error: relanzar con cuerpo legible
        raise _ue.HTTPError(e.url, e.code, f'{e.reason} - {body}', e.headers, None)


def _ensure_leverage(symbol, leverage):
    try:
        utils.fut_signed('POST', '/fapi/v1/leverage', {'symbol': symbol, 'leverage': leverage})
    except Exception:
        pass


def _get_fill_price(order_id, symbol, fallback):
    """Consulta la orden hasta obtener el avgPrice real (puede llegar en 0 en la respuesta inicial)."""
    for _ in range(5):
        try:
            d = utils.fut_signed('GET', '/fapi/v1/order', {'symbol': symbol, 'orderId': order_id})
            avg = float(d.get('avgPrice', 0))
            if avg > 0:
                return avg
        except Exception:
            pass
        time.sleep(1)
    return fallback


def open_short(candidate, state, max_shorts=None):
    """
    Abre una posición short en futures.
    TP: orden LIMIT reduceOnly.
    SL: gestionado por manage_short() via monitoreo de precio + cierre MARKET.
    """
    sym = candidate['symbol']

    available = utils.get_usdt_futures()
    capital   = utils.get_futures_capital_per_position(state)
    limits = capital_manager.get_limits()
    futures_usable = capital_manager.futures_usable_capital(available, limits)
    max_shorts = max_shorts if max_shorts is not None else utils.get_max_short_positions(futures_usable)
    ok_capacity, capacity_msg, _, _ = utils.validate_position_capacity(state, 'short', max_shorts)
    if not ok_capacity:
        return None, capacity_msg
    # Reducir capital si el token es volátil/riesgoso
    if candidate.get('risky'):
        capital = capital * config.RISKY_RISK_FACTOR

    try:
        filters  = utils.get_futures_filters(sym)
        step     = filters.get('step_size', 0.1)
        min_qty  = filters.get('min_qty', 0.1)
        min_not  = filters.get('min_notional', 5.0)
        tick     = filters.get('tick_size', 0.001)
    except Exception as e:
        return None, f'Error en filtros futures {sym}: {e}'

    # Obtener precio actual
    price = utils.get_fut_price(sym)

    # Dry-run: simular sin ejecutar
    if config.DRY_RUN:
        atr_v = candidate['atr']
        real_sl = utils.round_tick(price + config.SL_ATR_MULT_SHORT * atr_v, tick)
        real_tp = utils.round_tick(price - config.TP_ATR_MULT * atr_v, tick)
        notional_dry = capital * config.FUTURES_LEVERAGE
        qty_dry = utils.round_step(notional_dry / price, step)
        requested_margin = (qty_dry * price) / config.FUTURES_LEVERAGE
        try:
            ok, limit_msg, _ = capital_manager.validate_futures_order(
                state, available, requested_margin, max_shorts
            )
        except Exception as e:
            return None, f'CAPITAL LIMIT ERROR FUTURES: {e}'
        if not ok:
            return None, limit_msg
        pos = {
            'id': f'short_{sym}_{int(time.time())}_DRY',
            'direction': 'short', 'symbol': sym,
            'entry_price': price, 'quantity': qty_dry,
            'sl': real_sl, 'tp': real_tp, 'atr': atr_v,
            'tp_order_id': 'DRY',
            'entry_time': int(time.time()), 'partial_taken': False,
            'trail_trough': price, 'leverage': config.FUTURES_LEVERAGE,
            'dry_run': True,
        }
        return pos, f'[DRY-RUN] SHORT {sym} @ ${price:.4f} SL=${real_sl:.4f} TP=${real_tp:.4f}'

    notional = capital * config.FUTURES_LEVERAGE
    qty      = utils.round_step(notional / price, step)

    if qty < min_qty:
        return None, f'Cantidad mínima en futures no alcanzada: {qty} < {min_qty}'
    if qty * price < min_not:
        return None, f'Notional mínimo futures no alcanzado: ${qty * price:.2f} < ${min_not}'
    requested_margin = (qty * price) / config.FUTURES_LEVERAGE
    try:
        ok, limit_msg, _ = capital_manager.validate_futures_order(
            state, available, requested_margin, max_shorts
        )
    except Exception as e:
        return None, f'CAPITAL LIMIT ERROR FUTURES: {e}'
    if not ok:
        return None, limit_msg

    _ensure_leverage(sym, config.FUTURES_LEVERAGE)

    # MARKET SELL (abrir short) con backoff ante errores transitorios de API
    order = None
    last_err = None
    for _attempt in range(4):
        order_params = {
            'symbol':   sym,
            'side':     'SELL',
            'type':     'MARKET',
            'quantity': str(qty),
        }
        try:
            order = utils.fut_signed('POST', '/fapi/v1/order', order_params)
            break  # éxito
        except _ue.HTTPError as e:
            details = utils.log_binance_http_error('futures market sell', sym, 'SELL', 'MARKET', order_params, e)
            last_err = (
                f'HTTP {details.get("status")} code={details.get("code")} msg={details.get("msg")}'
                if details.get('code') is not None or details.get('msg') else str(e)
            )
            # Errores no retriables: balance insuficiente, par inválido, etc.
            _code = details.get('code') or 0
            if _code in (-2019, -1121, -1100, -1102):  # errores definitivos
                break
            if _attempt < 3:
                _delay = 10 * (2 ** _attempt)  # 10s, 20s, 40s
                import logging
                logging.warning(f'SHORT {sym}: intento {_attempt+1} fallido ({last_err}), reintentando en {_delay}s')
                time.sleep(_delay)
        except Exception as e:
            last_err = str(e)
            if _attempt < 3:
                _delay = 10 * (2 ** _attempt)
                import logging
                logging.warning(f'SHORT {sym}: intento {_attempt+1} fallido ({e}), reintentando en {_delay}s')
                time.sleep(_delay)
    if order is None:
        return None, f'Error al abrir short {sym} tras 4 intentos: {last_err}'

    order_id    = order.get('orderId')
    actual_price = _get_fill_price(order_id, sym, price)
    actual_qty  = float(order.get('origQty', qty))

    # Esperar a que Binance registre la posición
    for _ in range(6):
        time.sleep(1)
        try:
            check = utils.fut_signed('GET', '/fapi/v2/positionRisk', {'symbol': sym})
            if any(float(p.get('positionAmt', 0)) < 0 for p in check):
                break
        except Exception:
            pass

    # Calcular SL/TP desde precio real de fill
    atr_v   = candidate['atr']
    real_sl = utils.round_tick(actual_price + config.SL_ATR_MULT_SHORT * atr_v, tick)
    real_tp = utils.round_tick(actual_price - config.TP_ATR_MULT * atr_v, tick)
    real_tp = max(real_tp, tick)

    # Distancia mínima SL
    sl_dist_pct = (real_sl - actual_price) / actual_price * 100
    if sl_dist_pct < config.SL_MIN_DIST_PCT:
        real_sl = utils.round_tick(actual_price * (1 + config.SL_MIN_DIST_PCT / 100), tick)

    # Cancelar órdenes abiertas previas del símbolo
    try:
        open_orders = utils.fut_signed('GET', '/fapi/v1/openOrders', {'symbol': sym})
        for o in open_orders:
            try:
                utils.fut_signed('DELETE', '/fapi/v1/order', {'symbol': sym, 'orderId': o['orderId']})
            except Exception:
                pass
    except Exception:
        pass

    # TP como LIMIT + reduceOnly
    tp_order_id = ''
    try:
        tp_order = utils.fut_signed('POST', '/fapi/v1/order', {
            'symbol':     sym,
            'side':       'BUY',
            'type':       'LIMIT',
            'price':      str(real_tp),
            'quantity':   str(actual_qty),
            'reduceOnly': 'true',
            'timeInForce':'GTC',
        })
        tp_order_id = str(tp_order.get('orderId', ''))
    except Exception as e:
        utils.send_alert(f'⚠️ TP futures {sym} no se pudo colocar: {e}. SL gestionado por el bot.')

    # SL nativo: STOP_MARKET en el exchange (más robusto que el guardian software)
    sl_order_id = ''
    if config.NATIVE_SL_ENABLED:
        try:
            # Validación: stopPrice no debe exceder ~4.5% del precio actual
            # Binance rechaza STOP_MARKET si stopPrice > markPrice +5% (varía por símbolo)
            max_stop_dist_pct = 4.5
            max_allowed_sl = actual_price * (1 + max_stop_dist_pct / 100)
            if real_sl > max_allowed_sl:
                # Ajustar SL al máximo permitido
                real_sl = utils.round_tick(max_allowed_sl, tick)
                utils.send_alert(f'⚠️ {sym}: SL ajustado a {real_sl:.4f} (máx {max_stop_dist_pct}% sobre precio)')
            
            sl_order = _place_stop_market(sym, 'BUY', real_sl, actual_qty)
            sl_order_id = str(sl_order.get('orderId', '') or sl_order.get('strategyId', '')) if sl_order else ''
        except Exception as e:
            import logging
            error_msg = str(e)
            logging.error(f'SL nativo {sym}: stopPrice={real_sl}, qty={actual_qty}, price={actual_price}, error={error_msg}')
            utils.send_alert(f'⚠️ SL nativo {sym} no se pudo colocar: {error_msg}. Guardian software activo como fallback.')

    pos = {
        'id':            f'short_{sym}_{int(time.time())}',
        'direction':     'short',
        'symbol':        sym,
        'entry_price':   actual_price,
        'quantity':      actual_qty,
        'sl':            real_sl,
        'tp':            real_tp,
        'atr':           atr_v,
        'tp_order_id':   tp_order_id,
        'sl_order_id':   sl_order_id,   # SL nativo (vacío si falló o está desactivado)
        'entry_time':    int(time.time()),
        'partial_taken': False,
        'trail_trough':  actual_price,
        'leverage':      config.FUTURES_LEVERAGE,
    }

    fee    = actual_qty * actual_price * config.FUTURES_FEE_RATE
    pnl_tp = round((actual_price - real_tp) * actual_qty - fee * 2, 4)
    pnl_sl = round((actual_price - real_sl) * actual_qty - fee * 2, 4)
    sl_type = 'STOP_MARKET nativo' if sl_order_id else 'guardian software'

    msg = (
        f'📉 SHORT abierto: {sym} (x{config.FUTURES_LEVERAGE})\n'
        f'Entrada: ${actual_price:.4f} | Qty: {actual_qty}\n'
        f'SL: ${real_sl:.4f} ({sl_type}) | TP: ${real_tp:.4f} (LIMIT)\n'
        f'Si TP: +${pnl_tp:.4f} | Si SL: ${pnl_sl:.4f}'
    )
    return pos, msg


def manage_short(pos, state):
    """
    Gestiona una posición short activa cada ciclo.
    - Verifica si la posición sigue abierta en Binance
    - SL: si precio >= sl, cierra con MARKET
    - TP: monitoreado via orden LIMIT (también chequeamos posición real)
    - Trailing stop
    - Stale exit
    Retorna (acción, precio_cierre, pnl)
    """
    sym   = pos['symbol']
    entry = pos['entry_price']
    qty   = pos['quantity']

    price_now = utils.get_fut_price(sym)

    # Verificar si la posición sigue abierta.
    # Fix #6 — flujo con SL nativo: si STOP_MARKET se ejecutó entre ciclos, positionAmt llega 0
    # acá y se retorna 'closed_sl'. _handle_close() agrega cooldown y limpia el state.
    # El guardian (sl_guardian.py) también lo detecta por positionAmt==0 y solo limpia el TP.
    # No hay doble-cierre porque manage_short sale por este bloque antes de llegar al check de SL.
    try:
        positions = utils.fut_signed('GET', '/fapi/v2/positionRisk', {'symbol': sym})
        pos_amt = next((abs(float(p['positionAmt'])) for p in positions
                        if float(p.get('positionAmt', 0)) < 0), 0.0)

        if pos_amt == 0:
            # Posición cerrada (TP ejecutado o liquidada)
            pnl = (entry - price_now) * qty * (1 - config.FUTURES_FEE_RATE * 2)
            _cancel_tp(pos)
            if price_now < entry:
                return 'closed_tp', price_now, pnl
            else:
                return 'closed_sl', price_now, abs(pnl) * -1
    except Exception:
        pass

    # SL check: guardian software actúa siempre que no haya SL nativo válido en el exchange.
    # Si hay sl_order_id registrado, verificamos que la orden siga activa.
    # Si fue cancelada externamente (por Binance o manually), limpiamos el ID y el guardian toma control.
    sl = pos['sl']
    sl_order_id = pos.get('sl_order_id', '')

    if sl_order_id:
        # Verificar que la orden nativa sigue activa en el exchange
        try:
            order_info = utils.fut_signed('GET', '/fapi/v1/order', {
                'symbol': sym, 'orderId': int(sl_order_id)
            })
            status = order_info.get('status', '')
            if status not in ('NEW', 'PARTIALLY_FILLED'):
                # La orden ya no está activa (cancelada, expirada, o ejecutada sin cerrar posición)
                pos['sl_order_id'] = ''
                sl_order_id = ''
        except Exception:
            # Si no podemos verificar, asumimos que no existe y activamos el guardian
            pos['sl_order_id'] = ''
            sl_order_id = ''

    if not sl_order_id and price_now >= sl:
        # Guardian software: SL nativo no existe o fue cancelado
        if pos.get('dry_run'):
            pnl = (pos['entry_price'] - price_now) * qty * (1 - config.FUTURES_FEE_RATE * 2)
            return 'closed_sl', price_now, pnl
        pnl = _close_short_market(sym, qty, entry, price_now)
        _cancel_tp(pos)
        return 'closed_sl', price_now, pnl

    # Stale exit por tiempo máximo (12h) — aunque esté en profit
    elapsed_h  = (time.time() - pos.get('entry_time', time.time())) / 3600
    if elapsed_h > config.STALE_MAX_HOURS:
        pnl = _close_short_market(sym, qty, entry, price_now)
        _cancel_tp(pos)
        return 'closed_manual', price_now, pnl
    
    # Stale exit por poco movimiento (<0.5% en 5h)
    price_pct  = abs(price_now - entry) / entry * 100
    if elapsed_h > config.STALE_HOURS and price_pct < config.STALE_RANGE_PCT:
        pnl = _close_short_market(sym, qty, entry, price_now)
        _cancel_tp(pos)
        return 'closed_manual', price_now, pnl

    # Trailing stop (bajar SL a medida que el precio baja)
    trail_trough = pos.get('trail_trough', entry)
    if price_now < trail_trough * (1 - config.TRAIL_STEP_PCT / 100):
        atr_v  = pos.get('atr', abs(sl - entry))
        new_sl = price_now + config.SL_ATR_MULT_SHORT * atr_v
        if new_sl < sl:
            try:
                tick   = utils.get_futures_filters(sym).get('tick_size', 0.001)
                new_sl = utils.round_tick(new_sl, tick)
                pos['sl'] = new_sl
                pos['trail_trough'] = price_now
                # Actualizar SL nativo si existe: cancelar el viejo y poner uno nuevo
                if sl_order_id and config.NATIVE_SL_ENABLED:
                    new_id = _replace_native_sl(sym, sl_order_id, new_sl, qty)
                    if new_id:
                        pos['sl_order_id'] = new_id
                return 'updated', price_now, 0
            except Exception:
                pass

    return 'hold', price_now, 0


def _replace_native_sl(symbol, old_order_id, new_sl_price, qty):
    """Cancela el SL nativo anterior y coloca uno nuevo. Retorna el nuevo orderId o ''."""
    # Cancelar viejo
    if old_order_id:
        try:
            utils.fut_signed('DELETE', '/fapi/v1/order', {
                'symbol': symbol, 'orderId': int(old_order_id)
            })
        except Exception:
            pass
    # Colocar nuevo
    try:
        tick = utils.get_futures_filters(symbol).get('tick_size', 0.001)
        new_sl_price = utils.round_tick(new_sl_price, tick)
        
        # Validación: stopPrice no debe exceder ~4.5% del precio actual
        price_now = utils.get_fut_price(symbol)
        max_stop_dist_pct = 4.5
        max_allowed_sl = price_now * (1 + max_stop_dist_pct / 100)
        if new_sl_price > max_allowed_sl:
            new_sl_price = utils.round_tick(max_allowed_sl, tick)
        
        order = _place_stop_market(symbol, 'BUY', new_sl_price, qty)
        return str(order.get('orderId', '') or order.get('strategyId', '')) if order else ''
    except Exception as e:
        import logging
        error_msg = str(e)
        logging.error(f'Update SL nativo {symbol}: stopPrice={new_sl_price}, qty={qty}, error={error_msg}')
        utils.send_alert(f'⚠️ No se pudo actualizar SL nativo {symbol}: {error_msg}')
        return ''


def _update_native_sl(symbol, order_id, new_sl_price, qty):
    """Alias eliminado — usar _replace_native_sl directamente."""
    pass  # mantenido solo por compatibilidad; la lógica vive en _replace_native_sl


def _close_short_market(symbol, qty, entry, price_now):
    """Cierra short con MARKET. Retorna PnL aproximado."""
    try:
        order = utils.fut_signed('POST', '/fapi/v1/order', {
            'symbol':     symbol,
            'side':       'BUY',
            'type':       'MARKET',
            'quantity':   str(qty),
            'reduceOnly': 'true',
        })
        fill = float(order.get('avgPrice', price_now))
        if fill == 0:
            # Esperar fill real
            time.sleep(2)
            d = utils.fut_signed('GET', '/fapi/v1/order', {
                'symbol': symbol, 'orderId': order['orderId']
            })
            fill = float(d.get('avgPrice', price_now))
        pnl = (entry - fill) * qty * (1 - config.FUTURES_FEE_RATE * 2)
        return pnl
    except Exception:
        return (entry - price_now) * qty * (1 - config.FUTURES_FEE_RATE * 2)


def _cancel_tp(pos):
    """Cancela la orden TP LIMIT si existe."""
    tp_id = pos.get('tp_order_id', '')
    sym   = pos['symbol']
    if tp_id:
        try:
            utils.fut_signed('DELETE', '/fapi/v1/order', {
                'symbol':  sym,
                'orderId': int(tp_id),
            })
        except Exception:
            pass
