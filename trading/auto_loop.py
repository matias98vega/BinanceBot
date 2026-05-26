#!/usr/bin/env python3
"""
Auto Trading Loop — verifica si la OCO se cerró y si sí, analiza y recompra.
Corre cada 30 min via cron.

SAFETY RULES:
  1. Si compra exitosa pero OCO falla → 3 reintentos con backoff, luego MARKET SELL de emergencia
  2. Si estado=in_position y oco_order_list_id vacío → intentar recolocar OCO con SL/TP del estado
  3. Si oco_order_list_id sigue vacío después de retry → MARKET SELL de emergencia y alertar
  4. Nunca crashear por ValueError en oco_order_list_id vacío
"""
import json, urllib.request, urllib.parse, urllib.error, hmac, hashlib, time, math, os, sys

# ── Config ──────────────────────────────────────────────────────────────────
STATE_FILE = '/root/.openclaw/workspace/trading/state.json'
LOCK_FILE  = '/tmp/auto_loop.lock'
TRADES_LOG = '/root/.openclaw/workspace/trading/trades_log.txt'
TOOLS_KEY  = '0DwLCZ1RnGhfnWygp3PUxPrLGLjLByukBFvjEo06p5fVQpsICjdcKBLBRwXzOnVr'
TOOLS_SEC  = 'VCMhz7vCQZGgwAIV4PDY74bpRGOxDY0gT4rh6a5cLJmh2mCfcJF1uQu3qhzcQWmM'
BASE       = 'https://api.binance.com'
RISK_PCT   = 0.93    # usar 93% del capital disponible por trade
SL_ATR_MULT = 1.0
TP_ATR_MULT = 2.0
OCO_MAX_RETRIES = 3  # intentos para colocar OCO antes de vender en mercado
TRAIL_STEP_PCT   = 1.0   # subir SL cada vez que el precio sube 1% desde la entrada
RSI_MAX_ENTRY    = 65    # no entrar si RSI > 65 (sobrecomprado)
ATR_MIN_PCT      = 0.5   # no entrar si ATR < 0.5% del precio (mercado muy plano)
DAILY_LOSS_LIMIT = 3.0   # pausar si PnL del dia cae mas de $3 USDT
COOLDOWN_AFTER_SL = True # no reentrar en el mismo par inmediatamente despues de un SL

# ── Helpers ──────────────────────────────────────────────────────────────────
def log_trade(trade_num, symbol, result, pnl, capital_after):
    """Agrega una línea al log de trades cerrados."""
    pair    = symbol.replace('USDT', '/USDT')
    date    = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())
    pnl_str = f"+${pnl:.4f}" if pnl >= 0 else f"-${abs(pnl):.4f}"
    line    = f"{trade_num:<3}| {pair:<12}| {result:<8}| {pnl_str:<12}| ${capital_after:<10.4f}| {date}\n"
    with open(TRADES_LOG, 'a') as f:
        f.write(line)

def load_state():
    with open(STATE_FILE) as f:
        return json.load(f)

def save_state(s):
    s['last_update'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    with open(STATE_FILE, 'w') as f:
        json.dump(s, f, indent=2)

def signed_request(method, path, params=None):
    params = params or {}
    params['timestamp'] = int(time.time() * 1000)
    qs = urllib.parse.urlencode(params)
    sig = hmac.new(TOOLS_SEC.encode(), qs.encode(), hashlib.sha256).hexdigest()
    full_qs = f"{qs}&signature={sig}"
    if method in ('GET', 'DELETE'):
        url  = f"{BASE}{path}?{full_qs}"
        data = None
    else:
        url  = f"{BASE}{path}"
        data = full_qs.encode()
    req = urllib.request.Request(url, data=data, method=method,
          headers={'X-MBX-APIKEY': TOOLS_KEY, 'Content-Type': 'application/x-www-form-urlencoded'})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def public_get(path, params=None):
    qs = urllib.parse.urlencode(params or {})
    url = f"{BASE}{path}?{qs}" if qs else f"{BASE}{path}"
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())

def get_price(symbol):
    d = public_get('/api/v3/ticker/price', {'symbol': symbol})
    return float(d['price'])

def get_usdt_balance():
    d = signed_request('GET', '/api/v3/account')
    for b in d.get('balances', []):
        if b['asset'] == 'USDT':
            return float(b['free'])
    return 0.0

def get_asset_balance(asset):
    d = signed_request('GET', '/api/v3/account')
    for b in d.get('balances', []):
        if b['asset'] == asset:
            return float(b['free'])
    return 0.0

def get_klines(symbol, interval='1h', limit=48):
    return public_get('/api/v3/klines', {'symbol': symbol, 'interval': interval, 'limit': limit})

def ema(prices, period):
    k = 2/(period+1)
    e = [prices[0]]
    for p in prices[1:]:
        e.append(p*k + e[-1]*(1-k))
    return e

def rsi(prices, period=14):
    gains, losses = [], []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    if len(gains) < period: return 50
    ag = sum(gains[:period])/period
    al = sum(losses[:period])/period
    if al == 0: return 100
    return 100 - 100/(1+ag/al)

def macd_hist(prices):
    e12 = ema(prices, 12); e26 = ema(prices, 26)
    line = [a-b for a,b in zip(e12,e26)]
    signal = ema(line, 9)
    return line[-1] - signal[-1]

def score_symbol(symbol):
    try:
        klines = get_klines(symbol)
        closes = [float(k[4]) for k in klines]
        highs  = [float(k[2]) for k in klines]
        lows   = [float(k[3]) for k in klines]
        vols   = [float(k[5]) for k in klines]
        last   = closes[-1]
        e20    = ema(closes, 20)[-1]
        e50    = ema(closes, 50)[-1] if len(closes) >= 50 else e20
        rsi_v  = rsi(closes)
        mh     = macd_hist(closes)
        atr    = sum(h-l for h,l in zip(highs[-14:], lows[-14:])) / 14
        vol_r  = vols[-1] / (sum(vols[-10:])/10)
        sc = 0
        if last > e20:  sc += 2
        if last > e50:  sc += 1
        if 38 < rsi_v < 65: sc += 2
        elif rsi_v < 38:    sc += 1
        if mh > 0:      sc += 2
        if vol_r > 1.1: sc += 1
        sl = round(last - SL_ATR_MULT * atr, 4)
        tp = round(last + TP_ATR_MULT * atr, 4)
        atr_pct = (atr / last) * 100
        return {'symbol': symbol, 'score': sc, 'price': last, 'atr': atr,
                'atr_pct': atr_pct, 'sl': sl, 'tp': tp, 'rsi': rsi_v}
    except:
        return None

def get_step_size(symbol):
    info = public_get('/api/v3/exchangeInfo', {'symbol': symbol})
    for f in info['symbols'][0]['filters']:
        if f['filterType'] == 'LOT_SIZE':
            return float(f['stepSize']), float(f['minQty'])
    return 1.0, 1.0

def get_tick_size(symbol):
    info = public_get('/api/v3/exchangeInfo', {'symbol': symbol})
    for f in info['symbols'][0]['filters']:
        if f['filterType'] == 'PRICE_FILTER':
            return float(f['tickSize'])
    return 0.001

def floor_qty(qty, step):
    if step == 0: return qty
    factor = round(1/step)
    return math.floor(qty * factor) / factor

def round_price(price, tick):
    if tick == 0: return price
    factor = round(1/tick)
    return round(math.floor(price * factor) / factor, 10)

def place_oco(symbol, qty, tp, sl):
    step, _ = get_step_size(symbol)
    tick = get_tick_size(symbol)
    qty_floored = floor_qty(qty, step)
    if step >= 0.1:
        qty_str = f"{qty_floored:.1f}"
    elif step >= 0.01:
        qty_str = f"{qty_floored:.2f}"
    else:
        qty_str = f"{qty_floored:.4f}"
    tp_r     = round_price(tp, tick)
    sl_r     = round_price(sl, tick)
    sl_limit = round_price(sl - tick, tick)
    tp_str       = f"{tp_r:.8f}".rstrip('0').rstrip('.')
    sl_str       = f"{sl_r:.8f}".rstrip('0').rstrip('.')
    sl_limit_str = f"{sl_limit:.8f}".rstrip('0').rstrip('.')

    # Validar contra precio actual antes de enviar
    current = get_price(symbol)
    if tp_r <= current:
        raise ValueError(f"TP {tp_r} <= precio actual {current}")
    if sl_r >= current:
        raise ValueError(f"SL {sl_r} >= precio actual {current}")
    if sl_limit >= sl_r:
        raise ValueError(f"SL limit {sl_limit} >= SL stop {sl_r}")

    d = signed_request('POST', '/api/v3/order/oco', {
        'symbol': symbol, 'side': 'SELL',
        'quantity': qty_str,
        'price': tp_str,
        'stopPrice': sl_str,
        'stopLimitPrice': sl_limit_str,
        'stopLimitTimeInForce': 'GTC',
    })
    return d

def place_oco_with_retry(symbol, qty, tp, sl, max_retries=OCO_MAX_RETRIES):
    """Intenta colocar OCO hasta max_retries veces con backoff exponencial."""
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            oco = place_oco(symbol, qty, tp, sl)
            oco_id   = str(oco.get('orderListId', ''))
            oco_oids = [str(o['orderId']) for o in oco.get('orders', [])]
            if oco_id:
                return oco_id, oco_oids, None
        except urllib.error.HTTPError as ex:
            body = ex.read().decode()
            last_err = Exception(f'HTTP {ex.code}: {body}')
            sys.stderr.write(f'[OCO attempt {attempt}] {last_err}\n')
            if attempt < max_retries:
                time.sleep(2 ** attempt)
        except Exception as ex:
            last_err = ex
            sys.stderr.write(f'[OCO attempt {attempt}] {ex}\n')
            if attempt < max_retries:
                time.sleep(2 ** attempt)
    return '', [], last_err

def market_sell_all(symbol):
    """Vende todo el balance disponible del asset en mercado. Safety net."""
    asset = symbol.replace('USDT', '')
    step, min_qty = get_step_size(symbol)
    balance = get_asset_balance(asset)
    qty = floor_qty(balance, step)
    if qty < min_qty:
        return None, f"Balance {balance} < minQty {min_qty}"
    try:
        d = signed_request('POST', '/api/v3/order', {
            'symbol': symbol, 'side': 'SELL', 'type': 'MARKET', 'quantity': f"{qty}"
        })
        return d, None
    except Exception as ex:
        return None, str(ex)

def cancel_oco(symbol, order_list_id):
    """Cancela un OCO existente. Retorna True si OK."""
    try:
        signed_request('DELETE', '/api/v3/orderList', {
            'symbol': symbol,
            'orderListId': int(order_list_id)
        })
        return True
    except Exception as ex:
        sys.stderr.write(f'[cancel_oco] {ex}\n')
        return False

def update_trailing_stop(state, current_price):
    """
    Si el precio subió TRAIL_STEP_PCT% o más desde la entrada,
    sube el SL para asegurar ganancia. Cancela el OCO viejo y pone uno nuevo.
    Retorna (nuevo_sl, mensaje) o (None, None) si no hay que actualizar.
    """
    entry   = state['entry_price']
    sl      = state['sl']
    tp      = state['tp']
    sym     = state['symbol']
    qty     = state['quantity']
    gain_pct = (current_price - entry) / entry * 100

    # Calcular cuántos steps de TRAIL_STEP_PCT llevamos
    steps = int(gain_pct / TRAIL_STEP_PCT)
    if steps < 1:
        return None, None  # aún no llegó al primer step

    # Nuevo SL: entrada + (steps - 1) * TRAIL_STEP_PCT %
    # Con steps=1 (precio subió 1-2%): SL sube a breakeven
    # Con steps=2 (precio subió 2-3%): SL asegura 1% de ganancia
    new_sl_pct = (steps - 1) * TRAIL_STEP_PCT / 100
    new_sl = round(entry * (1 + new_sl_pct), 8)
    tick   = get_tick_size(sym)
    new_sl = round_price(new_sl, tick)

    if new_sl <= sl:
        return None, None  # el SL ya está en ese nivel o más alto

    # Cancelar OCO viejo
    if not cancel_oco(sym, state['oco_order_list_id']):
        return None, None  # si no se puede cancelar, no tocar nada

    # Colocar nuevo OCO con SL actualizado
    oco_id, oco_oids, err = place_oco_with_retry(sym, qty, tp, new_sl)
    if not oco_id:
        # Falló — intentar restaurar el OCO original
        place_oco_with_retry(sym, qty, tp, sl)
        return None, f'Trail falló ({err}), OCO restaurado'

    state['sl']                = new_sl
    state['oco_order_list_id'] = oco_id
    state['oco_order_ids']     = oco_oids
    msg = f'📈 Trailing stop actualizado: SL ${sl:.4f} → ${new_sl:.4f} | Ganancia asegurada: {new_sl_pct*100:.1f}%'
    return new_sl, msg


    step, min_qty = get_step_size(symbol)
    price = get_price(symbol)
    qty = floor_qty((usdt_amount / price) * 0.999, step)  # 0.1% margen comisión
    if qty < min_qty:
        return None, 0
    d = signed_request('POST', '/api/v3/order', {
        'symbol': symbol, 'side': 'BUY', 'type': 'MARKET', 'quantity': f"{qty}"
    })
    return d, qty

def check_oco_status(order_list_id):
    """order_list_id puede ser int o str; acepta ambos."""
    for attempt in range(3):
        try:
            d = signed_request('GET', '/api/v3/orderList', {'orderListId': int(order_list_id)})
            return d.get('listOrderStatus', 'UNKNOWN')
        except urllib.error.HTTPError as e:
            if e.code == 400:
                return 'NOT_FOUND'  # OCO no existe
            if attempt < 2:
                time.sleep(2)
        except Exception:
            if attempt < 2:
                time.sleep(2)
    return 'UNKNOWN'

def analyze_market(skip_symbol=None):
    candidates = ['WLDUSDT','NEARUSDT','RENDERUSDT','TONUSDT','SOLUSDT',
                  'BNBUSDT','ETHUSDT','BTCUSDT','FETUSDT','INJUSDT']
    tickers = public_get('/api/v3/ticker/24hr')
    usdt_tickers = {t['symbol']: t for t in tickers
                    if t['symbol'].endswith('USDT') and float(t.get('quoteVolume',0)) > 20e6}
    results = []
    for sym in candidates:
        if sym == skip_symbol:          # cooldown: saltar par que dio SL
            continue
        if sym not in usdt_tickers:
            continue
        chg = float(usdt_tickers[sym]['priceChangePercent'])
        if chg < -5:
            continue
        r = score_symbol(sym)
        if not r:
            continue
        # Filtro RSI: no entrar sobrecomprado
        if r['rsi'] > RSI_MAX_ENTRY:
            continue
        # Filtro ATR: no entrar si mercado muy plano
        if r['atr_pct'] < ATR_MIN_PCT:
            continue
        if r['score'] >= 5:
            results.append(r)
    if not results:
        for sym in ['ETHUSDT', 'SOLUSDT', 'BNBUSDT']:
            if sym == skip_symbol: continue
            r = score_symbol(sym)
            if r and r['rsi'] <= RSI_MAX_ENTRY and r['atr_pct'] >= ATR_MIN_PCT:
                results.append(r)
    results.sort(key=lambda x: x['score'], reverse=True)
    return results[0] if results else None

# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    state = load_state()
    output = []

    # ── 1. Posición abierta ───────────────────────────────────────────────────
    if state['status'] == 'in_position':
        sym   = state['symbol']
        oco_id = state.get('oco_order_list_id', '').strip()

        # CASO A: No hay OCO — intentar recolocarla
        if not oco_id:
            output.append(f"⚠️ Posición abierta en {sym} SIN OCO. Intentando recolocar...")
            qty = state.get('quantity', 0)
            sl  = state.get('sl', 0)
            tp  = state.get('tp', 0)

            if qty > 0 and sl > 0 and tp > 0:
                oco_id, oco_oids, err = place_oco_with_retry(sym, qty, tp, sl)
                if oco_id:
                    state['oco_order_list_id'] = oco_id
                    state['oco_order_ids']      = oco_oids
                    output.append(f"✅ OCO recolocada | SL ${sl:.4f} | TP ${tp:.4f} | ID {oco_id}")
                    save_state(state)
                    print('\n'.join(output))
                    return
                else:
                    # Fallo total — vender en mercado para proteger capital
                    output.append(f"🚨 OCO falló {OCO_MAX_RETRIES} veces ({err}). Ejecutando MARKET SELL de emergencia...")
                    sell_order, sell_err = market_sell_all(sym)
                    if sell_order:
                        usdt_now = get_usdt_balance()
                        pnl = usdt_now - (state['capital_usdt'] - state['total_pnl_usdt'])
                        state['total_pnl_usdt'] = round(state['total_pnl_usdt'] + pnl, 4)
                        state['capital_usdt']   = round(usdt_now, 4)
                        state['status']         = 'scanning'
                        state['oco_order_list_id'] = ''
                        output.append(f"💰 MARKET SELL ejecutado | PnL: {'+' if pnl>=0 else ''}{pnl:.4f} USDT | Capital: ${usdt_now:.4f}")
                    else:
                        output.append(f"🚨🚨 MARKET SELL también falló: {sell_err}. ACCIÓN MANUAL REQUERIDA.")
                        state['status'] = 'ERROR_MANUAL_REQUIRED'
                    save_state(state)
                    print('\n'.join(output))
                    return
            else:
                output.append(f"🚨 Estado incompleto (qty={qty}, sl={sl}, tp={tp}). No puedo recolocar OCO. REVISIÓN MANUAL.")
                state['status'] = 'ERROR_MANUAL_REQUIRED'
                save_state(state)
                print('\n'.join(output))
                return

        # CASO B: OCO existe — verificar estado normal
        oco_status = check_oco_status(oco_id)
        current_price = get_price(sym)
        entry = state['entry_price']
        pnl_pct = (current_price - entry) / entry * 100

        if oco_status in ('ALL_DONE', 'FILLED'):
            usdt_now = get_usdt_balance()
            pnl = usdt_now - (state['capital_usdt'] - state['total_pnl_usdt'])
            result = 'TP ✅' if pnl > 0 else 'SL 🛑'
            state['total_pnl_usdt'] = round(state['total_pnl_usdt'] + pnl, 4)
            state['capital_usdt']   = round(usdt_now, 4)
            state['status']         = 'scanning'
            state['oco_order_list_id'] = ''
            state['oco_order_ids']     = []
            # Cooldown: si fue SL, recordar el par para no reentrar enseguida
            if pnl <= 0 and COOLDOWN_AFTER_SL:
                state['cooldown_symbol'] = sym
            else:
                state['cooldown_symbol'] = ''
            log_trade(state.get('trade_count', '?'), sym, result, pnl, usdt_now)
            output.append(f"{result} — {sym} cerrado | PnL: {'+' if pnl>=0 else ''}{pnl:.4f} USDT | Acumulado: {state['total_pnl_usdt']:+.4f} USDT")
            output.append(f"Capital disponible: ${usdt_now:.4f} | Analizando mercado para próxima entrada...")
        else:
            # Sigue activa — intentar trailing stop, luego reportar
            new_sl, trail_msg = update_trailing_stop(state, current_price)
            if trail_msg:
                output.append(trail_msg)
            output.append(f"📊 {sym} = ${current_price:.4f} | {pnl_pct:+.2f}% desde entrada | SL ${state['sl']:.4f} | TP ${state['tp']:.4f} | {time.strftime('%H:%M UTC', time.gmtime())}")
            save_state(state)
            print('\n'.join(output))
            return

    # ── 2. Scanning: buscar nueva oportunidad ─────────────────────────────────
    if state['status'] == 'scanning':
        # Chequeo de perdida diaria
        if state['total_pnl_usdt'] <= -DAILY_LOSS_LIMIT:
            output.append(f"⚠️ Limite de perdida diaria alcanzado (${state['total_pnl_usdt']:.4f}). Bot pausado hasta reinicio manual.")
            state['status'] = 'paused'
            save_state(state)
            print('\n'.join(output))
            return

        usdt_balance = get_usdt_balance()
        if usdt_balance < 5.0:
            output.append(f"⚠️ Capital insuficiente (${usdt_balance:.4f}) para nueva entrada. Minimo $5 USDT.")
            state['status'] = 'paused'
            save_state(state)
            print('\n'.join(output))
            return

        skip_sym = state.get('cooldown_symbol', '') if COOLDOWN_AFTER_SL else ''
        best = analyze_market(skip_symbol=skip_sym)
        if skip_sym and not best:
            output.append(f"🔄 Sin candidatos excluyendo {skip_sym} (cooldown SL). Ampliando busqueda...")
            best = analyze_market()
        state['cooldown_symbol'] = ''  # limpiar cooldown tras usarlo
        if not best:
            output.append("🔍 Sin candidatos claros ahora. Reintento en 30 min.")
            save_state(state)
            print('\n'.join(output))
            return

        sym = best['symbol']
        output.append(f"🎯 Mejor candidato: {sym} | Score {best['score']}/8 | RSI {best['rsi']:.0f} | ${best['price']:.4f}")

        # Comprar
        invest = round(usdt_balance * RISK_PCT, 4)
        order, qty = place_market_buy(sym, invest)
        if not order or qty == 0:
            output.append(f"❌ Error al comprar {sym}. Reintento en 30 min.")
            save_state(state)
            print('\n'.join(output))
            return

        actual_price = get_price(sym)
        output.append(f"✅ Compra ejecutada: {qty} {sym.replace('USDT','')} a ~${actual_price:.4f}")

        # Colocar OCO con reintentos
        time.sleep(1)
        oco_id, oco_oids, oco_err = place_oco_with_retry(sym, qty, best['tp'], best['sl'])

        if oco_id:
            output.append(f"🔒 OCO colocada: SL ${best['sl']:.4f} | TP ${best['tp']:.4f}")
        else:
            # OCO falló a pesar de los reintentos → vender en mercado inmediatamente
            output.append(f"🚨 OCO falló tras {OCO_MAX_RETRIES} intentos ({oco_err}). MARKET SELL de emergencia...")
            sell_order, sell_err = market_sell_all(sym)
            if sell_order:
                usdt_now = get_usdt_balance()
                pnl_emg  = usdt_now - (state['capital_usdt'] - state['total_pnl_usdt'])
                log_trade(state.get('trade_count', '?'), sym, 'SELL🚨', pnl_emg, usdt_now)
                output.append(f"💰 Posición cerrada en mercado. Capital recuperado: ${usdt_now:.4f}")
                state['capital_usdt'] = round(usdt_now, 4)
            else:
                output.append(f"🚨🚨 MARKET SELL falló: {sell_err}. ACCIÓN MANUAL URGENTE.")
                # Guardar estado con oco vacío para que el próximo ciclo intente recolocar
                state.update({
                    'status': 'in_position',
                    'symbol': sym,
                    'entry_price': actual_price,
                    'quantity': qty,
                    'sl': best['sl'],
                    'tp': best['tp'],
                    'oco_order_list_id': '',
                    'oco_order_ids': [],
                    'trade_count': state.get('trade_count', 0) + 1,
                })
            save_state(state)
            print('\n'.join(output))
            return

        # Actualizar estado exitoso
        state.update({
            'status': 'in_position',
            'symbol': sym,
            'entry_price': actual_price,
            'quantity': qty,
            'sl': best['sl'],
            'tp': best['tp'],
            'oco_order_list_id': oco_id,
            'oco_order_ids': oco_oids,
            'trade_count': state.get('trade_count', 0) + 1,
        })
        output.append(f"📈 Trade #{state['trade_count']} activo | Capital total acumulado PnL: {state['total_pnl_usdt']:+.4f} USDT")

    save_state(state)
    print('\n'.join(output))

if __name__ == '__main__':
    # Lock para evitar ejecuciones solapadas
    import fcntl
    lock_fd = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('⚠️ Ya hay una instancia corriendo. Saliendo.')
        sys.exit(0)
    try:
        main()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
