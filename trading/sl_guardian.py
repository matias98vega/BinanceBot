#!/usr/bin/env python3
"""
SL Guardian — corre cada 2 min via cron.
Solo verifica si alguna posición activa tocó su SL y cierra con MARKET.
Ultra liviano: no hace análisis, no abre posiciones.
"""
import sys, os, time, json
sys.path.insert(0, os.path.dirname(__file__))
import utils, config
from analytics import AnalyticsLogger

ANALYTICS = AnalyticsLogger()

def main():
    lock = utils.acquire_lock()
    if not lock:
        # Si el bot principal está corriendo, esperar un poco y reintentar
        time.sleep(5)
        lock = utils.acquire_lock()
        if not lock:
            print('HEARTBEAT_OK')
            sys.exit(0)

    try:
        _run()
    finally:
        utils.release_lock(lock)


def _run():
    state = utils.load_state()
    positions = state.get('positions', [])

    if not positions:
        print('HEARTBEAT_OK')
        return

    triggered = []
    critical_alerts = []   # alertas que sí se notifican al usuario

    for pos in positions:
        direction = pos['direction']
        sym       = pos['symbol']
        sl        = pos['sl']
        entry     = pos['entry_price']
        qty       = pos['quantity']

        try:
            if direction == 'long':
                # Long spot: el OCO se encarga, pero si por algún motivo no hay OCO → chequear
                oco_id = pos.get('oco_order_list_id', '')
                if oco_id:
                    continue  # OCO activo, Binance lo maneja solo

                price = utils.get_spot_price(sym)
                if price <= sl:
                    partial_info = ''
                    if pos.get('partial_taken'):
                        ppnl = pos.get('partial_pnl')
                        ppnl_str = f'+${ppnl:.4f}' if ppnl is not None else 'registrado'
                        partial_info = f' (parcial TP ya cobrado: {ppnl_str} — esta mitad sale en breakeven ✅)'
                    msg = f'🛡️ GUARDIAN SL LONG {sym}: precio {price:.4f} <= SL {sl:.4f} → cerrando{partial_info}'
                    print(msg)
                    critical_alerts.append(msg)
                    utils.send_alert(msg)
                    _close_spot_market(sym, qty)
                    pnl = (price - entry) * qty
                    triggered.append((pos.get('id'), sym, 'long', price, pnl))

            elif direction == 'short':
                price = utils.get_fut_price(sym)
                dist_pct = (sl - price) / price * 100

                if price >= sl:
                    # Si hay SL nativo registrado, es posible que ya se ejecutó en el exchange.
                    # El bot.py / manage_short lo detecta por positionAmt==0.
                    # Guardian como fallback: solo actuar si no hay SL nativo O si la posición sigue abierta.
                    sl_order_id = pos.get('sl_order_id', '')
                    pos_still_open = True
                    if sl_order_id:
                        try:
                            positions_check = utils.fut_signed('GET', '/fapi/v2/positionRisk', {'symbol': sym})
                            pos_amt = next((abs(float(p['positionAmt'])) for p in positions_check
                                            if float(p.get('positionAmt', 0)) < 0), 0.0)
                            pos_still_open = pos_amt > 0
                        except Exception:
                            pass

                    if pos_still_open:
                        partial_info = ''
                        if pos.get('partial_taken'):
                            ppnl = pos.get('partial_pnl')
                            ppnl_str = f'+${ppnl:.4f}' if ppnl is not None else 'registrado'
                            partial_info = f' (parcial TP ya cobrado: {ppnl_str} \u2014 esta mitad sale en breakeven \u2705)'
                        msg = f'\ud83d\udee1\ufe0f GUARDIAN SL SHORT {sym}: precio {price:.4f} >= SL {sl:.4f} \u2192 cerrando{partial_info}'
                        print(msg)
                        critical_alerts.append(msg)
                        utils.send_alert(msg)
                        pnl = _close_fut_market(sym, qty, entry, price)
                        # Cancelar TP y SL nativo si existen
                        tp_id = pos.get('tp_order_id', '')
                        if tp_id:
                            try:
                                utils.fut_signed('DELETE', '/fapi/v1/order', {
                                    'symbol': sym, 'orderId': int(tp_id)
                                })
                            except Exception:
                                pass
                        if sl_order_id:
                            try:
                                utils.fut_signed('DELETE', '/fapi/v1/order', {
                                    'symbol': sym, 'orderId': int(sl_order_id)
                                })
                            except Exception:
                                pass
                        triggered.append((pos.get('id'), sym, 'short', price, pnl))
                    else:
                        # SL nativo ya cerró la posición — solo limpiar state
                        msg = f'🛡️ SL nativo ejecutado {sym} → limpiando state'
                        print(msg)
                        critical_alerts.append(msg)
                        utils.send_alert(msg)
                        pnl = (entry - price) * qty * (1 - config.FUTURES_FEE_RATE * 2)
                        # Cancelar TP
                        tp_id = pos.get('tp_order_id', '')
                        if tp_id:
                            try:
                                utils.fut_signed('DELETE', '/fapi/v1/order', {
                                    'symbol': sym, 'orderId': int(tp_id)
                                })
                            except Exception:
                                pass
                        triggered.append((pos.get('id'), sym, 'short', price, pnl))
                elif dist_pct < 0.5:
                    # Muy cerca del SL — alerta de atención (con contexto de partial)
                    partial_info = ', ya cobró parcial TP (riesgo reducido 50%)' if pos.get('partial_taken') else ''
                    msg = f'⚠️ {sym} a {dist_pct:.2f}% del SL${sl:.4f}{partial_info}'
                    print(msg)
                    critical_alerts.append(msg)
                # else: monitoreo silencioso, no imprimir nada

        except Exception as e:
            print(f'  ⚠️ Error al chequear {sym}: {e}')

    # Actualizar state si hubo cierres
    if triggered:
        positions_new = []
        triggered_ids = {t[0] for t in triggered if t[0]}
        triggered_syms = {t[1] for t in triggered}
        positions_by_id = {p.get('id'): p for p in positions}

        for pos in positions:
            if pos.get('id') not in triggered_ids and pos['symbol'] not in triggered_syms:
                positions_new.append(pos)

        for trade_id, sym, direction, price, pnl in triggered:
            original_pos = positions_by_id.get(trade_id, {})
            state['trade_count']    = state.get('trade_count', 0) + 1
            state['total_pnl_usdt'] = round(state.get('total_pnl_usdt', 0) + pnl, 4)
            state['daily_pnl_usdt'] = round(state.get('daily_pnl_usdt', 0) + pnl, 4)
            state['consec_sl']      = state.get('consec_sl', 0) + 1
            if config.COOLDOWN_AFTER_SL:
                utils.add_cooldown(state, sym)
            try:
                ANALYTICS.log_trade_close(
                    trade_id=trade_id,
                    symbol=sym,
                    side=direction.upper(),
                    entry_time=original_pos.get('entry_time'),
                    entry_price=original_pos.get('entry_price'),
                    exit_price=price,
                    exit_reason='GUARDIAN_SL',
                    pnl_usdt=pnl,
                )
            except Exception:
                pass

        state['positions'] = positions_new
        utils.save_state(state)
    else:
        # Sin SL tocados — solo alertar si hay critical_alerts (SL muy cerca)
        if not critical_alerts:
            print('HEARTBEAT_OK')
        # Si hay critical_alerts, ya se imprimieron arriba y el agente los entrega


def _close_spot_market(symbol, qty):
    try:
        utils.spot_signed('POST', '/api/v3/order', {
            'symbol': symbol, 'side': 'SELL', 'type': 'MARKET', 'quantity': str(qty)
        })
    except Exception as e:
        utils.send_alert(f'🚨 Guardian no pudo cerrar LARGO {symbol}: {e}')


def _close_fut_market(symbol, qty, entry, price_now):
    try:
        order = utils.fut_signed('POST', '/fapi/v1/order', {
            'symbol': symbol, 'side': 'BUY', 'type': 'MARKET',
            'quantity': str(qty), 'reduceOnly': 'true',
        })
        time.sleep(2)
        d = utils.fut_signed('GET', '/fapi/v1/order', {
            'symbol': symbol, 'orderId': order['orderId']
        })
        fill = float(d.get('avgPrice', price_now))
        if fill == 0:
            fill = price_now
        return (entry - fill) * qty * (1 - config.FUTURES_FEE_RATE * 2)
    except Exception as e:
        utils.send_alert(f'🚨 Guardian no pudo cerrar CORTO {symbol}: {e}')
        return (entry - price_now) * qty * (1 - config.FUTURES_FEE_RATE * 2)


if __name__ == '__main__':
    main()
