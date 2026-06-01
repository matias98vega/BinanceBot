#!/usr/bin/env python3
"""
Módulo SHORT — gestión de posiciones short en Futures (fapi).
SL: monitoreado por precio y cerrado con MARKET (STOP_MARKET no disponible en esta cuenta).
TP: orden LIMIT + reduceOnly=true.
"""
import sys, os, time, math
sys.path.insert(0, os.path.dirname(__file__))
import utils, config


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


def open_short(candidate, state):
    """
    Abre una posición short en futures.
    TP: orden LIMIT reduceOnly.
    SL: gestionado por manage_short() via monitoreo de precio + cierre MARKET.
    """
    sym = candidate['symbol']

    available = utils.get_usdt_futures()
    risk_pct  = utils.get_futures_risk_pct(available)
    capital   = available * risk_pct

    try:
        filters  = utils.get_futures_filters(sym)
        step     = filters.get('step_size', 0.1)
        min_qty  = filters.get('min_qty', 0.1)
        min_not  = filters.get('min_notional', 5.0)
        tick     = filters.get('tick_size', 0.001)
    except Exception as e:
        return None, f'Error en filtros futures {sym}: {e}'

    _ensure_leverage(sym, config.FUTURES_LEVERAGE)

    # Obtener precio actual
    price = utils.get_fut_price(sym)

    # Dry-run: simular sin ejecutar
    if config.DRY_RUN:
        atr_v = candidate['atr']
        real_sl = utils.round_tick(price + config.SL_ATR_MULT * atr_v, tick)
        real_tp = utils.round_tick(price - config.TP_ATR_MULT * atr_v, tick)
        notional_dry = available * config.FUTURES_RISK_PCT * config.FUTURES_LEVERAGE
        qty_dry = utils.round_step(notional_dry / price, step)
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

    # MARKET SELL (abrir short)
    try:
        order = utils.fut_signed('POST', '/fapi/v1/order', {
            'symbol':   sym,
            'side':     'SELL',
            'type':     'MARKET',
            'quantity': str(qty),
        })
    except Exception as e:
        return None, f'Error al abrir short en futures {sym}: {e}'

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
    real_sl = utils.round_tick(actual_price + config.SL_ATR_MULT * atr_v, tick)
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
        'entry_time':    int(time.time()),
        'partial_taken': False,
        'trail_trough':  actual_price,
        'leverage':      config.FUTURES_LEVERAGE,
    }

    fee    = actual_qty * actual_price * config.FUTURES_FEE_RATE
    pnl_tp = round((actual_price - real_tp) * actual_qty - fee * 2, 4)
    pnl_sl = round((actual_price - real_sl) * actual_qty - fee * 2, 4)

    msg = (
        f'📉 SHORT abierto: {sym} (x{config.FUTURES_LEVERAGE})\n'
        f'Entrada: ${actual_price:.4f} | Qty: {actual_qty}\n'
        f'SL: ${real_sl:.4f} (bot) | TP: ${real_tp:.4f} (LIMIT)\n'
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

    # Verificar si la posición sigue abierta
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

    # SL check: si precio subió hasta el SL → cerrar
    sl = pos['sl']
    if price_now >= sl:
        if pos.get('dry_run'):
            pnl = (pos['entry_price'] - price_now) * qty * (1 - config.FUTURES_FEE_RATE * 2)
            return 'closed_sl', price_now, pnl
        pnl = _close_short_market(sym, qty, entry, price_now)
        _cancel_tp(pos)
        return 'closed_sl', price_now, pnl

    # Stale exit
    elapsed_h  = (time.time() - pos.get('entry_time', time.time())) / 3600
    price_pct  = abs(price_now - entry) / entry * 100
    if elapsed_h > config.STALE_HOURS and price_pct < config.STALE_RANGE_PCT:
        pnl = _close_short_market(sym, qty, entry, price_now)
        _cancel_tp(pos)
        return 'closed_manual', price_now, pnl

    # Trailing stop (bajar SL a medida que el precio baja)
    trail_trough = pos.get('trail_trough', entry)
    if price_now < trail_trough * (1 - config.TRAIL_STEP_PCT / 100):
        atr_v  = pos.get('atr', abs(sl - entry))
        new_sl = price_now + config.SL_ATR_MULT * atr_v
        if new_sl < sl:
            try:
                tick   = utils.get_futures_filters(sym).get('tick_size', 0.001)
                new_sl = utils.round_tick(new_sl, tick)
                pos['sl']           = new_sl
                pos['trail_trough'] = price_now
                return 'updated', price_now, 0
            except Exception:
                pass

    return 'hold', price_now, 0


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
