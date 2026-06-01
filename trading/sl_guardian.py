#!/usr/bin/env python3
"""
SL Guardian — corre cada 2 min via cron.
Solo verifica si alguna posición activa tocó su SL y cierra con MARKET.
Ultra liviano: no hace análisis, no abre posiciones.
"""
import sys, os, time, json
sys.path.insert(0, os.path.dirname(__file__))
import utils, config

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
                    print(f'🛡️ GUARDIAN SL LARGO {sym}: precio {price:.4f} <= SL {sl:.4f} → cerrando')
                    _close_spot_market(sym, qty)
                    pnl = (price - entry) * qty
                    triggered.append((sym, 'long', price, pnl))

            elif direction == 'short':
                price = utils.get_fut_price(sym)
                dist_pct = (sl - price) / price * 100

                if price >= sl:
                    print(f'🛡️ GUARDIAN SL CORTO {sym}: precio {price:.4f} >= SL {sl:.4f} → cerrando')
                    pnl = _close_fut_market(sym, qty, entry, price)
                    # Cancelar TP
                    tp_id = pos.get('tp_order_id', '')
                    if tp_id:
                        try:
                            utils.fut_signed('DELETE', '/fapi/v1/order', {
                                'symbol': sym, 'orderId': int(tp_id)
                            })
                        except Exception:
                            pass
                    triggered.append((sym, 'short', price, pnl))
                else:
                    print(f'  📉 {sym}: ${price:.4f} | SL ${sl:.4f} | distancia {dist_pct:.2f}%')

        except Exception as e:
            print(f'  ⚠️ Error al chequear {sym}: {e}')

    # Actualizar state si hubo cierres
    if triggered:
        positions_new = []
        triggered_syms = {t[0] for t in triggered}

        for pos in positions:
            if pos['symbol'] not in triggered_syms:
                positions_new.append(pos)

        for sym, direction, price, pnl in triggered:
            state['trade_count']    = state.get('trade_count', 0) + 1
            state['total_pnl_usdt'] = round(state.get('total_pnl_usdt', 0) + pnl, 4)
            state['daily_pnl_usdt'] = round(state.get('daily_pnl_usdt', 0) + pnl, 4)
            state['consec_sl']      = state.get('consec_sl', 0) + 1
            if config.COOLDOWN_AFTER_SL:
                utils.add_cooldown(state, sym)

        state['positions'] = positions_new
        utils.save_state(state)
    else:
        # Sin SL tocados → silencio total (HEARTBEAT_OK suprime la notificación)
        print('HEARTBEAT_OK')


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
