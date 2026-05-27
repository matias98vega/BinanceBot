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
TRADES_LOG   = '/root/.openclaw/workspace/trading/trades_log.txt'
ANALYSIS_LOG = '/root/.openclaw/workspace/trading/analysis_log.txt'
# Credenciales desde .env (nunca hardcodear en el codigo)
def _load_env():
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    env = {}
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env
_env = _load_env()
TOOLS_KEY  = _env.get('BINANCE_API_KEY', os.environ.get('BINANCE_API_KEY', ''))
TOOLS_SEC  = _env.get('BINANCE_API_SECRET', os.environ.get('BINANCE_API_SECRET', ''))
BASE       = 'https://api.binance.com'
RISK_PCT   = 0.93    # usar 93% del capital disponible por trade
SL_ATR_MULT = 1.0
TP_ATR_MULT = 2.0
OCO_MAX_RETRIES = 3  # intentos para colocar OCO antes de vender en mercado
TRAIL_STEP_PCT   = 1.0   # subir SL cada vez que el precio sube 1% desde la entrada
RSI_MAX_ENTRY    = 65    # no entrar si RSI > 65 (sobrecomprado)
ATR_MIN_PCT      = 0.5   # no entrar si ATR < 0.5% del precio (mercado muy plano)
SL_MIN_DIST_PCT  = 1.0   # distancia minima del SL desde entrada (1%)
DAILY_LOSS_LIMIT = 3.0   # pausar si PnL del dia cae mas de $3 USDT
COOLDOWN_AFTER_SL  = True  # no reentrar en el mismo par inmediatamente despues de un SL
PARTIAL_TAKE_PCT   = 0.5   # tomar ganancia parcial cuando el precio llega al 50% del recorrido TP
STALE_HOURS        = 8     # salir si el trade lleva +8h y precio está entre -0.5% y +0.5% desde entrada
STALE_RANGE_PCT    = 0.5   # rango de "estancado" en %
ALERT_TARGET       = '20313075:thread:019e6042-4a09-7808-a0e4-dea13cade83b'
BNB_FEE_RATE       = 0.00075  # 0.075% por lado con BNB (0.1% sin BNB); round-trip ~0.15%
RISK_PCT_REDUCED   = 0.50    # capital a usar tras 2 SL consecutivos
MAX_CONSEC_SL      = 2       # cantidad de SL seguidos antes de reducir riesgo

# ── Helpers ──────────────────────────────────────────────────────────────────
def log_trade(trade_num, symbol, result, pnl, capital_after):
    """Agrega una línea al log de trades cerrados."""
    pair    = symbol.replace('USDT', '/USDT')
    date    = time.strftime('%Y-%m-%d %H:%M UY', time.gmtime(time.time() - 3*3600))
    pnl_str = f"+${pnl:.4f}" if pnl >= 0 else f"-${abs(pnl):.4f}"
    line    = f"{trade_num:<3}| {pair:<12}| {result:<8}| {pnl_str:<12}| ${capital_after:<10.4f}| {date}\n"
    with open(TRADES_LOG, 'a') as f:
        f.write(line)

def log_analysis(chosen, descarte):
    """Guarda un registro de cada escaneo de mercado."""
    now = time.strftime('%Y-%m-%d %H:%M UY', time.gmtime(time.time() - 3*3600))
    try:
        with open(ANALYSIS_LOG, 'a') as f:
            if chosen:
                f.write(f"[{now}] ELEGIDO: {chosen['symbol']} score={chosen['score']} RSI={chosen['rsi']:.0f} ATR={chosen['atr_pct']:.2f}%\n")
            else:
                f.write(f"[{now}] SIN CANDIDATO\n")
            for sym, motivo in descarte.items():
                f.write(f"  ✗ {sym}: {motivo}\n")
    except Exception:
        pass

def send_alert(msg):
    """Manda mensaje proactivo via Jarvis. Silencia errores para no crashear el bot."""
    import subprocess
    try:
        subprocess.run([
            'openclaw', 'message', 'send',
            '--channel', 'jarvis',
            '--target', ALERT_TARGET,
            '--message', msg
        ], timeout=10, capture_output=True)
    except Exception:
        pass  # nunca crashear por fallo de alerta

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
        # Timeframe 1h (entrada)
        klines = get_klines(symbol, interval='1h', limit=48)
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

        # Confirmacion 4h: tendencia mayor
        klines_4h = get_klines(symbol, interval='4h', limit=24)
        closes_4h = [float(k[4]) for k in klines_4h]
        highs_4h  = [float(k[2]) for k in klines_4h]
        lows_4h   = [float(k[3]) for k in klines_4h]
        e20_4h    = ema(closes_4h, 20)[-1]
        atr_4h    = sum(h-l for h,l in zip(highs_4h[-14:], lows_4h[-14:])) / 14
        trend_ok  = last > e20_4h  # precio por encima de EMA20 en 4h

        # Score base
        sc = 0
        if last > e20:  sc += 2
        if last > e50:  sc += 1
        if 38 < rsi_v < 65: sc += 2
        elif rsi_v < 38:    sc += 1
        if mh > 0:      sc += 2
        if vol_r > 1.1: sc += 1
        if trend_ok:    sc += 1  # bonus confirmacion 4h

        # Score minimo dinamico: si mercado muy volatil (ATR 4h alto), exigir mas
        atr_4h_pct = (atr_4h / last) * 100
        min_score = 6 if atr_4h_pct > 3.0 else 5  # mercado volatil = mas selectivo

        sl = round(last - SL_ATR_MULT * atr, 4)
        tp = round(last + TP_ATR_MULT * atr, 4)
        atr_pct = (atr / last) * 100
        return {'symbol': symbol, 'score': sc, 'price': last, 'atr': atr,
                'atr_pct': atr_pct, 'sl': sl, 'tp': tp, 'rsi': rsi_v,
                'trend_4h': trend_ok, 'min_score': min_score}
    except:
        return None

_exchange_info_cache = {}

def get_exchange_info(symbol):
    if symbol not in _exchange_info_cache:
        _exchange_info_cache[symbol] = public_get('/api/v3/exchangeInfo', {'symbol': symbol})
    return _exchange_info_cache[symbol]

def get_step_size(symbol):
    info = get_exchange_info(symbol)
    for f in info['symbols'][0]['filters']:
        if f['filterType'] == 'LOT_SIZE':
            return float(f['stepSize']), float(f['minQty'])
    return 1.0, 1.0

def get_tick_size(symbol):
    info = get_exchange_info(symbol)
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
    sl_dist_pct = (current - sl_r) / current * 100
    if sl_dist_pct < SL_MIN_DIST_PCT:
        raise ValueError(f"SL demasiado cerca: {sl_dist_pct:.2f}% (minimo {SL_MIN_DIST_PCT}%)")

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

def take_partial_profit(state, current_price, output):
    """
    Si el precio llega al 50% del recorrido hacia el TP:
    - Vende la mitad en mercado
    - Recoloca OCO con la mitad restante y SL en breakeven
    Retorna True si se ejecuto la parcial, False si no.
    """
    if state.get('partial_taken'):
        return False  # ya se tomo parcial en este trade

    entry = state['entry_price']
    tp    = state['tp']
    sl    = state['sl']
    sym   = state['symbol']
    qty   = state['quantity']

    recorrido  = tp - entry
    umbral     = entry + recorrido * PARTIAL_TAKE_PCT
    if current_price < umbral:
        return False  # aun no llego al 50%

    # Cancelar OCO actual
    if not cancel_oco(sym, state['oco_order_list_id']):
        output.append("⚠️ Parcial: no se pudo cancelar OCO. Manteniendo posicion completa.")
        return False

    # Vender la mitad en mercado
    step, min_qty = get_step_size(sym)
    qty_half = floor_qty(qty / 2, step)
    if qty_half < min_qty:
        # La mitad es demasiado chica — restaurar OCO original y salir
        place_oco_with_retry(sym, qty, tp, sl)
        output.append("⚠️ Parcial: cantidad minima no alcanzada. Manteniendo posicion completa.")
        return False

    try:
        signed_request('POST', '/api/v3/order', {
            'symbol': sym, 'side': 'SELL', 'type': 'MARKET', 'quantity': f"{qty_half}"
        })
    except Exception as ex:
        # Fallo la venta parcial — restaurar OCO original
        place_oco_with_retry(sym, qty, tp, sl)
        output.append(f"⚠️ Parcial: venta fallida ({ex}). OCO restaurado.")
        return False

    # Calcular PnL de la mitad vendida
    pnl_half = (current_price - entry) * qty_half
    output.append(f"💸 Ganancia parcial tomada: vendidas {qty_half} {sym.replace('USDT','')} a ${current_price:.4f} | PnL parcial: +${pnl_half:.4f}")

    # Nueva cantidad restante
    qty_rest = floor_qty(qty - qty_half, step)

    # Nuevo SL = breakeven (precio de entrada)
    tick = get_tick_size(sym)
    new_sl = round_price(entry, tick)

    # Colocar nuevo OCO con la mitad restante y SL en breakeven
    oco_id, oco_oids, err = place_oco_with_retry(sym, qty_rest, tp, new_sl)
    if not oco_id:
        output.append(f"🚨 Parcial: OCO restante fallo ({err}). MARKET SELL de emergencia...")
        market_sell_all(sym)
        return False

    state['quantity']          = qty_rest
    state['sl']                = new_sl
    state['oco_order_list_id'] = oco_id
    state['oco_order_ids']     = oco_oids
    state['partial_taken']     = True
    output.append(f"🔒 OCO actualizado: {qty_rest} {sym.replace('USDT','')} restantes | SL en breakeven ${new_sl:.4f} | TP ${tp:.4f}")
    return True

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

def place_market_buy(symbol, usdt_amount):
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

# Pares que nunca queremos operar (stablecoins, wrapped, etc.)
BLACKLIST = {'USDCUSDT','BUSDUSDT','TUSDUSDT','USDTUSDT','FDUSDUSDT',
             'WBTCUSDT','STETHUSDT','BETHUSDT','LDOUSDT'}

def analyze_market(skip_symbol=None):
    tickers = public_get('/api/v3/ticker/24hr')
    usdt_tickers = {t['symbol']: t for t in tickers
                    if t['symbol'].endswith('USDT')
                    and float(t.get('quoteVolume', 0)) > 20e6
                    and t['symbol'] not in BLACKLIST}

    # Top 20 por volumen 24h + lista fija como base garantizada
    base = ['WLDUSDT','NEARUSDT','RENDERUSDT','TONUSDT','SOLUSDT',
            'BNBUSDT','ETHUSDT','BTCUSDT','FETUSDT','INJUSDT']
    top20 = sorted(usdt_tickers.keys(),
                   key=lambda s: float(usdt_tickers[s].get('quoteVolume',0)),
                   reverse=True)[:20]
    candidates = list(dict.fromkeys(base + top20))  # union, sin duplicados, base primero

    results = []
    descarte = {}
    for sym in candidates:
        if sym == skip_symbol:
            descarte[sym] = 'cooldown SL'
            continue
        if sym not in usdt_tickers:
            descarte[sym] = 'volumen insuficiente (<$20M)'
            continue
        chg = float(usdt_tickers[sym]['priceChangePercent'])
        if chg < -5:
            descarte[sym] = f'caida fuerte 24h ({chg:.1f}%)'
            continue
        vol_24h = float(usdt_tickers[sym].get('quoteVolume', 0))
        try:
            kd = public_get('/api/v3/klines', {'symbol': sym, 'interval': '1d', 'limit': 4})
            avg_vol_3d = sum(float(k[7]) for k in kd[:-1]) / 3
            vol_growing = vol_24h > avg_vol_3d * 0.9
        except:
            vol_growing = True
        if not vol_growing:
            descarte[sym] = 'volumen decreciente vs 3d'
            continue
        r = score_symbol(sym)
        if not r:
            descarte[sym] = 'error al analizar'
            continue
        if r['rsi'] > RSI_MAX_ENTRY:
            descarte[sym] = f'RSI alto ({r["rsi"]:.0f})'
            continue
        if r['atr_pct'] < ATR_MIN_PCT:
            descarte[sym] = f'ATR bajo ({r["atr_pct"]:.2f}%)'
            continue
        if r['score'] < r.get('min_score', 5):
            descarte[sym] = f'score bajo ({r["score"]}/{r.get("min_score",5)})'
            continue
        results.append(r)
    if not results:
        # Reutilizar resultados ya calculados del loop si existen
        fallback_syms = ['ETHUSDT', 'SOLUSDT', 'BNBUSDT']
        scored = {r['symbol']: r for r in [score_symbol(s) for s in fallback_syms
                  if s not in [x['symbol'] for x in results] and s != skip_symbol
                  and s not in descarte] if r}
        # Tambien rescatar los que solo fueron descartados por score bajo
        for sym_f in fallback_syms:
            if sym_f == skip_symbol: continue
            r = scored.get(sym_f) or (score_symbol(sym_f) if sym_f in descarte and 'score bajo' in descarte.get(sym_f,'') else None)
            if r and r['rsi'] <= RSI_MAX_ENTRY and r['atr_pct'] >= ATR_MIN_PCT:
                results.append(r)
                descarte.pop(sym_f, None)
    results.sort(key=lambda x: x['score'], reverse=True)
    return (results[0] if results else None), descarte

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
                        send_alert(f"🚨🚨 BOT EN ERROR — {sym} tiene posicion abierta SIN STOPS. Market sell fallo: {sell_err}. INTERVENCION MANUAL URGENTE.")
                    save_state(state)
                    print('\n'.join(output))
                    return
            else:
                output.append(f"🚨 Estado incompleto (qty={qty}, sl={sl}, tp={tp}). No puedo recolocar OCO. REVISIÓN MANUAL.")
                state['status'] = 'ERROR_MANUAL_REQUIRED'
                send_alert(f"🚨 BOT EN ERROR — {sym} con estado incompleto. No pudo recolocar OCO. REVISION MANUAL urgente.")
                save_state(state)
                print('\n'.join(output))
                return

        # CASO B: OCO existe — verificar estado normal
        oco_status = check_oco_status(oco_id)
        current_price = get_price(sym)
        entry = state['entry_price']
        pnl_pct = (current_price - entry) / entry * 100

        # CASO B1: OCO desaparecio (ejecutado entre ciclos o cancelado externamente)
        if oco_status == 'NOT_FOUND':
            output.append(f"⚠️ OCO {oco_id} no encontrado en Binance. Verificando balance...")
            asset = sym.replace('USDT','')
            asset_bal = get_asset_balance(asset)
            usdt_bal  = get_usdt_balance()
            # Si no queda asset, el OCO se ejecuto (TP o SL)
            step, min_qty = get_step_size(sym)
            if asset_bal < min_qty:
                usdt_now = usdt_bal
                fee  = state.get('entry_price', 0) * state.get('quantity', 0) * BNB_FEE_RATE * 2
                pnl  = usdt_now - (state['capital_usdt'] - state['total_pnl_usdt']) - fee
                result = 'TP ✅' if pnl > 0 else 'SL 🛑'
                state['total_pnl_usdt']    = round(state['total_pnl_usdt'] + pnl, 4)
                state['capital_usdt']      = round(usdt_now, 4)
                state['status']            = 'scanning'
                state['oco_order_list_id'] = ''
                state['oco_order_ids']     = []
                state['partial_taken']     = False
                if pnl <= 0 and COOLDOWN_AFTER_SL:
                    state['cooldown_symbol'] = sym
                else:
                    state['cooldown_symbol'] = ''
                log_trade(state.get('trade_count','?'), sym, result, pnl, usdt_now)
                output.append(f"{result} — {sym} cerrado (detectado por balance) | PnL: {'+' if pnl>=0 else ''}{pnl:.4f} USDT | Acumulado: {state['total_pnl_usdt']:+.4f} USDT")
            else:
                # Todavia tiene el asset — OCO fue cancelado externamente, recolocar
                output.append(f"🔄 Asset {asset} todavia en cuenta ({asset_bal}). Recolocando OCO...")
                sl = state.get('sl', 0)
                tp = state.get('tp', 0)
                qty = state.get('quantity', asset_bal)
                oco_id_new, oco_oids, err = place_oco_with_retry(sym, qty, tp, sl)
                if oco_id_new:
                    state['oco_order_list_id'] = oco_id_new
                    state['oco_order_ids']     = oco_oids
                    output.append(f"✅ OCO recolocado: SL ${sl:.4f} | TP ${tp:.4f}")
                else:
                    send_alert(f"🚨 OCO de {sym} desaparecio y no se pudo recolocar: {err}. INTERVENCION MANUAL.")
                    output.append(f"🚨 No se pudo recolocar OCO: {err}. Alerta enviada.")
            save_state(state)
            print('\n'.join(output))
            return

        if oco_status in ('ALL_DONE', 'FILLED'):
            usdt_now = get_usdt_balance()
            fee  = state.get('entry_price', 0) * state.get('quantity', 0) * BNB_FEE_RATE * 2
            pnl  = usdt_now - (state['capital_usdt'] - state['total_pnl_usdt']) - fee
            result = 'TP ✅' if pnl > 0 else 'SL 🛑'
            state['total_pnl_usdt'] = round(state['total_pnl_usdt'] + pnl, 4)
            state['capital_usdt']   = round(usdt_now, 4)
            state['status']         = 'scanning'
            state['oco_order_list_id'] = ''
            state['oco_order_ids']     = []
            state['partial_taken']     = False  # reset para el proximo trade
            # Actualizar contador de SL consecutivos
            if pnl <= 0:
                state['consec_sl'] = state.get('consec_sl', 0) + 1
            else:
                state['consec_sl'] = 0  # reset al primer TP
            # Cooldown: si fue SL, recordar el par para no reentrar enseguida
            if pnl <= 0 and COOLDOWN_AFTER_SL:
                state['cooldown_symbol'] = sym
            else:
                state['cooldown_symbol'] = ''
            log_trade(state.get('trade_count', '?'), sym, result, pnl, usdt_now)
            output.append(f"{result} — {sym} cerrado | PnL: {'+' if pnl>=0 else ''}{pnl:.4f} USDT | Acumulado: {state['total_pnl_usdt']:+.4f} USDT")
            output.append(f"Capital disponible: ${usdt_now:.4f} | Analizando mercado para próxima entrada...")
        else:
            # Sigue activa — 1) chequeo estancado, 2) ganancia parcial, 3) trailing stop, 4) reportar
            hours_open = (time.time() - state.get('entry_time', time.time())) / 3600
            if hours_open >= STALE_HOURS and abs(pnl_pct) <= STALE_RANGE_PCT:
                output.append(f"⏰ Trade estancado {hours_open:.1f}h | PnL {pnl_pct:+.2f}% — saliendo en mercado...")
                cancel_oco(sym, state['oco_order_list_id'])
                sell_order, sell_err = market_sell_all(sym)
                if sell_order:
                    usdt_now = get_usdt_balance()
                    pnl = usdt_now - (state['capital_usdt'] - state['total_pnl_usdt'])
                    state['total_pnl_usdt'] = round(state['total_pnl_usdt'] + pnl, 4)
                    state['capital_usdt']   = round(usdt_now, 4)
                    state['status']         = 'scanning'
                    state['oco_order_list_id'] = ''
                    state['oco_order_ids']     = []
                    state['partial_taken']     = False
                    state['cooldown_symbol']   = sym  # cooldown tras salida por tiempo
                    log_trade(state.get('trade_count','?'), sym, 'STALE⏰', pnl, usdt_now)
                    output.append(f"✅ Salida por estancamiento | PnL: {'+' if pnl>=0 else ''}{pnl:.4f} USDT | Capital: ${usdt_now:.4f}")
                else:
                    output.append(f"🚨 Market sell falló: {sell_err}. Manteniendo posicion.")
                save_state(state)
                print('\n'.join(output))
                return

            take_partial_profit(state, current_price, output)
            new_sl, trail_msg = update_trailing_stop(state, current_price)
            if trail_msg:
                output.append(trail_msg)
            output.append(f"📊 {sym} = ${current_price:.4f} | {pnl_pct:+.2f}% desde entrada | {hours_open:.1f}h abierto | SL ${state['sl']:.4f} | TP ${state['tp']:.4f} | {time.strftime('%H:%M UY', time.gmtime(time.time() - 3*3600))}")
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
        best, descarte = analyze_market(skip_symbol=skip_sym)
        if skip_sym and not best:
            output.append(f"🔄 Sin candidatos excluyendo {skip_sym} (cooldown SL). Ampliando busqueda...")
            best, descarte = analyze_market()
        state['cooldown_symbol'] = ''  # limpiar cooldown tras usarlo
        log_analysis(best, descarte)
        if not best:
            output.append("🔍 Sin candidatos claros ahora. Reintento en 30 min.")
            for sym_d, motivo in descarte.items():
                output.append(f"  ↳ {sym_d}: {motivo}")
            save_state(state)
            print('\n'.join(output))
            return

        sym = best['symbol']
        output.append(f"🎯 Mejor candidato: {sym} | Score {best['score']}/8 | RSI {best['rsi']:.0f} | ${best['price']:.4f}")

        # Comprar — reducir riesgo si hay SL consecutivos
        consec_sl = state.get('consec_sl', 0)
        risk = RISK_PCT_REDUCED if consec_sl >= MAX_CONSEC_SL else RISK_PCT
        if consec_sl >= MAX_CONSEC_SL:
            output.append(f"⚠️ {consec_sl} SL consecutivos — operando con {int(risk*100)}% del capital")
        invest = round(usdt_balance * risk, 4)
        order, qty = place_market_buy(sym, invest)
        if not order or qty == 0:
            output.append(f"❌ Error al comprar {sym}. Reintento en 30 min.")
            save_state(state)
            print('\n'.join(output))
            return

        actual_price = get_price(sym)
        output.append(f"✅ Compra ejecutada: {qty} {sym.replace('USDT','')} a ~${actual_price:.4f}")

        # Recalcular SL/TP con precio real de ejecucion
        atr = best['atr']
        real_sl = round(actual_price - SL_ATR_MULT * atr, 4)
        real_tp = round(actual_price + TP_ATR_MULT * atr, 4)
        tick = get_tick_size(sym)
        real_sl = round_price(real_sl, tick)
        real_tp = round_price(real_tp, tick)

        # Colocar OCO con reintentos
        time.sleep(1)
        oco_id, oco_oids, oco_err = place_oco_with_retry(sym, qty, real_tp, real_sl)

        if oco_id:
            output.append(f"🔒 OCO colocada: SL ${real_sl:.4f} | TP ${real_tp:.4f}")
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
                send_alert(f"🚨🚨 BOT EN ERROR — {sym} comprado pero OCO y market sell fallaron. Posicion abierta SIN STOPS. INTERVENCION URGENTE.")
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
            'sl': real_sl,
            'tp': real_tp,
            'oco_order_list_id': oco_id,
            'oco_order_ids': oco_oids,
            'trade_count': state.get('trade_count', 0) + 1,
            'entry_time': int(time.time()),
            'partial_taken': False,
        })
        output.append(f"📈 Trade #{state['trade_count']} activo | Capital total acumulado PnL: {state['total_pnl_usdt']:+.4f} USDT")
        send_alert(
            f"📈 Trade #{state['trade_count']} abierto\n"
            f"Par: {sym}\n"
            f"Entrada: ${actual_price:.4f}\n"
            f"SL: ${real_sl:.4f} | TP: ${real_tp:.4f}\n"
            f"Capital: ${usdt_balance:.4f} USDT"
        )

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
