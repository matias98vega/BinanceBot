#!/usr/bin/env python3
"""
Análisis de mercado: contexto macro BTC, scoring de candidatos long y short.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import utils, config

# Fallback estático si la lista dinámica falla
_STATIC_CANDIDATES = [
    'SOLUSDT', 'BNBUSDT', 'ETHUSDT', 'ADAUSDT', 'DOTUSDT',
    'AVAXUSDT', 'LINKUSDT', 'NEARUSDT', 'MATICUSDT', 'LTCUSDT',
    'ATOMUSDT', 'UNIUSDT', 'AAVEUSDT', 'FTMUSDT', 'INJUSDT',
    'OPUSDT', 'ARBUSDT', 'STXUSDT', 'TIAUSDT', 'WLDUSDT',
    'SNXUSDT', 'LDOUSDT', 'SUIUSDT', 'APTUSDT', 'SEIUSDT',
]

# Cache para no re-fetchear en cada ciclo (TTL 1 hora)
_candidates_cache = {'ts': 0, 'symbols': []}

def get_dynamic_candidates(top_n=40):
    """
    Retorna los top_n pares USDT de futures ordenados por volumen 24h.
    Se actualiza cada hora; usa fallback estático si falla.
    """
    import time as _time
    global _candidates_cache

    if _time.time() - _candidates_cache['ts'] < 3600 and _candidates_cache['symbols']:
        return _candidates_cache['symbols']

    try:
        tickers = utils.fut_public('/fapi/v1/ticker/24hr')
        # Filtrar USDT perps, excluir stables y pares raros
        exclude = {'USDCUSDT', 'BUSDUSDT', 'TUSDUSDT', 'USDPUSDT', 'BTCSTUSDT'}
        valid = [
            t for t in tickers
            if t['symbol'].endswith('USDT')
            and t['symbol'] not in exclude
            and not t['symbol'].startswith('1000')   # micro-contracts
            and float(t.get('quoteVolume', 0)) > 5_000_000  # mín $5M vol 24h
        ]
        # Ordenar por volumen 24h descendente
        valid.sort(key=lambda x: float(x.get('quoteVolume', 0)), reverse=True)
        symbols = [t['symbol'] for t in valid[:top_n]]

        _candidates_cache = {'ts': _time.time(), 'symbols': symbols}
        return symbols
    except Exception:
        return _STATIC_CANDIDATES


def confirm_15m(symbol, direction, futures=False):
    """
    Filtro rápido en 15m: confirma que el momentum de corto plazo esté alineado.
    Para longs: las últimas 3 velas 15m deben mostrar momentum alcista.
    Para shorts: momentum bajista.
    Retorna (ok: bool, reason: str)
    """
    try:
        k15 = utils.get_klines(symbol, interval='15m', limit=20, futures=futures)
        c15 = [float(k[4]) for k in k15]
        h15 = [float(k[2]) for k in k15]
        l15 = [float(k[3]) for k in k15]
        v15 = [float(k[5]) for k in k15]

        last    = c15[-1]
        e9_15   = utils.ema(c15, 9)[-1]
        e21_15  = utils.ema(c15, 21)[-1]
        rsi_15  = utils.rsi(c15, 14)
        mh_15   = utils.macd_hist(c15)
        atr_15  = utils.atr(h15, l15, 7)

        # Velas recientes: cuántas son alcistas vs bajistas en últimas 4
        recent_bull = sum(1 for i in range(-4, 0) if c15[i] > c15[i-1])
        recent_bear = 4 - recent_bull

        if direction == 'long':
            ok = (
                last > e9_15           # precio sobre EMA9
                and mh_15 > 0          # MACD positivo en 15m
                and rsi_15 < 75        # no sobrecomprado extremo
                and recent_bull >= 2   # al menos 2 de 4 velas alcistas
            )
            reason = (f'15m: e9={e9_15:.4f} MACD={mh_15:+.5f} '
                      f'RSI={rsi_15:.0f} velas_alcistas={recent_bull}/4')
        else:  # short
            ok = (
                last < e9_15           # precio bajo EMA9
                and mh_15 < 0          # MACD negativo en 15m
                and rsi_15 > 25        # no sobrevendido extremo
                and recent_bear >= 2   # al menos 2 de 4 velas bajistas
            )
            reason = (f'15m: e9={e9_15:.4f} MACD={mh_15:+.5f} '
                      f'RSI={rsi_15:.0f} velas_bajistas={recent_bear}/4')

        return ok, reason
    except Exception as e:
        return True, f'15m check error (pass-through): {e}'  # si falla, no bloquear


def get_btc_context():
    """
    Analiza el contexto macro de BTC.
    Retorna un dict con:
      - trend:   'bullish' | 'bearish' | 'neutral'
      - btc_price, ema50_4h, ema20_4h
      - change_4h: cambio % en las últimas 4 velas 4h
      - change_1h: cambio % en la última vela 1h
      - force_mode: None | 'long_only' | 'short_only'
    """
    try:
        k4h = utils.get_klines('BTCUSDT', interval='4h', limit=52)
        closes_4h = [float(k[4]) for k in k4h]
        highs_4h  = [float(k[2]) for k in k4h]
        lows_4h   = [float(k[3]) for k in k4h]

        k1h = utils.get_klines('BTCUSDT', interval='1h', limit=2)
        price_1h_prev = float(k1h[-2][4])
        price_now     = float(k1h[-1][4])

        ema50_4h = utils.ema(closes_4h, 50)[-1]
        ema20_4h = utils.ema(closes_4h, 20)[-1]
        atr_4h   = utils.atr(highs_4h, lows_4h, 14)

        btc_price  = closes_4h[-1]
        change_4h  = (closes_4h[-1] - closes_4h[-5]) / closes_4h[-5] * 100  # últimas 4 velas
        change_1h  = (price_now - price_1h_prev) / price_1h_prev * 100

        # Tendencia: precio vs EMA50 y EMA20
        if btc_price > ema50_4h and btc_price > ema20_4h:
            trend = 'bullish'
        elif btc_price < ema50_4h and btc_price < ema20_4h:
            trend = 'bearish'
        else:
            trend = 'neutral'

        # Modo forzado si movimiento extremo
        force_mode = None
        if change_4h <= config.BTC_CRASH_PCT:
            force_mode = 'short_only'
        elif change_4h >= config.BTC_PUMP_PCT:
            force_mode = 'long_only'

        return {
            'trend':      trend,
            'btc_price':  btc_price,
            'ema50_4h':   ema50_4h,
            'ema20_4h':   ema20_4h,
            'atr_4h':     atr_4h,
            'change_4h':  change_4h,
            'change_1h':  change_1h,
            'force_mode': force_mode,
        }
    except Exception as e:
        return {
            'trend': 'neutral', 'btc_price': 0, 'ema50_4h': 0, 'ema20_4h': 0,
            'atr_4h': 0, 'change_4h': 0, 'change_1h': 0, 'force_mode': None,
            'error': str(e)
        }


def score_long(symbol, btc_ctx):
    """
    Evalúa un símbolo como candidato LONG (spot).
    Timeframes: 15m (momentum) + 1h (entrada) + 4h (tendencia).
    """
    try:
        k1h = utils.get_klines(symbol, interval='1h', limit=50)
        closes = [float(k[4]) for k in k1h]
        highs  = [float(k[2]) for k in k1h]
        lows   = [float(k[3]) for k in k1h]
        vols   = [float(k[5]) for k in k1h]

        last   = closes[-1]
        e20    = utils.ema(closes, 20)[-1]
        e50    = utils.ema(closes, 50)[-1] if len(closes) >= 50 else e20
        rsi_v  = utils.rsi(closes)
        mh     = utils.macd_hist(closes)
        atr_v  = utils.atr(highs, lows, 14)
        vol_r  = vols[-1] / (sum(vols[-10:]) / 10) if vols[-10:] else 1.0

        # Confirmación 4h
        k4h      = utils.get_klines(symbol, interval='4h', limit=24)
        c4h      = [float(k[4]) for k in k4h]
        e20_4h   = utils.ema(c4h, 20)[-1]
        h4h      = [float(k[2]) for k in k4h]
        l4h      = [float(k[3]) for k in k4h]
        atr_4h   = utils.atr(h4h, l4h, 14)
        trend_ok = last > e20_4h

        # Filtro 15m (momentum de corto plazo)
        ok_15m, reason_15m = confirm_15m(symbol, 'long', futures=False)

        sc = 0
        reasons = []
        if last > e20:    sc += 2; reasons.append('precio>EMA20')
        if last > e50:    sc += 1; reasons.append('precio>EMA50')
        if 38 < rsi_v < config.RSI_MAX_LONG:
            sc += 2; reasons.append(f'RSI={rsi_v:.0f} ok')
        elif rsi_v < 38:
            sc += 1; reasons.append(f'RSI={rsi_v:.0f} sobrevendido')
        if mh > 0:        sc += 2; reasons.append('MACD_1h+')
        if vol_r > 1.1:   sc += 1; reasons.append(f'vol={vol_r:.2f}x')
        if trend_ok:      sc += 1; reasons.append('tendencia_4h ok')
        if ok_15m:        sc += 2; reasons.append('15m ok')
        else:             reasons.append(f'15m ❌')

        atr_pct    = (atr_v / last) * 100
        atr_4h_pct = (atr_4h / last) * 100
        min_score  = config.SCORE_MIN_VOLATILE if atr_4h_pct > config.ATR_VOLATILE_THRESH else config.SCORE_MIN

        # Si el 15m falla explícitamente, subir el min_score requerido
        if not ok_15m:
            min_score = max(min_score, config.SCORE_MIN + 2)

        sl = round(last - config.SL_ATR_MULT * atr_v, 8)
        tp = round(last + config.TP_ATR_MULT * atr_v, 8)

        sl_dist_pct = (last - sl) / last * 100
        if sl_dist_pct < config.SL_MIN_DIST_PCT:
            sl = round(last * (1 - config.SL_MIN_DIST_PCT / 100), 8)

        # Correlación BTC si BTC está débil
        corr_btc = 0.0
        if btc_ctx['change_4h'] < config.BTC_WEAK_PCT and symbol != 'BTCUSDT':
            try:
                btc_k   = utils.get_klines('BTCUSDT', interval='1h', limit=48)
                btc_cls = [float(k[4]) for k in btc_k]
                n = min(len(closes), len(btc_cls))
                corr_btc = utils.pearson_corr(closes[-n:], btc_cls[-n:])
            except Exception:
                pass

        return {
            'symbol':    symbol,
            'direction': 'long',
            'score':     sc,
            'min_score': min_score,
            'price':     last,
            'sl':        sl,
            'tp':        tp,
            'rsi':       rsi_v,
            'atr':       atr_v,
            'atr_pct':   atr_pct,
            'trend_4h':  trend_ok,
            'ok_15m':    ok_15m,
            'corr_btc':  corr_btc,
            'reasons':   reasons,
        }
    except Exception:
        return None


def score_short(symbol, btc_ctx):
    """
    Evalúa un símbolo como candidato SHORT (futures).
    Busca setups bajistas: precio bajo EMAs, RSI alto/divergente, MACD negativo,
    death cross, volumen en distribución.
    """
    try:
        k1h = utils.get_klines(symbol, interval='1h', limit=60, futures=True)
        closes = [float(k[4]) for k in k1h]
        highs  = [float(k[2]) for k in k1h]
        lows   = [float(k[3]) for k in k1h]
        vols   = [float(k[5]) for k in k1h]

        last   = closes[-1]
        e20    = utils.ema(closes, 20)[-1]
        e50    = utils.ema(closes, 50)[-1] if len(closes) >= 50 else e20
        rsi_v  = utils.rsi(closes)
        mh     = utils.macd_hist(closes)
        atr_v  = utils.atr(highs, lows, 14)
        vol_r  = vols[-1] / (sum(vols[-10:]) / 10) if vols[-10:] else 1.0

        # Death cross en 1h: EMA20 cruza por debajo de EMA50
        ema20_series = utils.ema(closes, 20)
        ema50_series = utils.ema(closes, 50) if len(closes) >= 50 else ema20_series
        death_cross  = (ema20_series[-1] < ema50_series[-1] and
                        ema20_series[-3] >= ema50_series[-3])

        # Divergencia bajista RSI: precio hace higher high pero RSI hace lower high
        rsi_series = [utils.rsi(closes[:i]) for i in range(30, len(closes)+1, 5)]
        bearish_div = False
        if len(rsi_series) >= 3:
            price_hh = closes[-1] > closes[-6]      # precio subió
            rsi_lh   = rsi_series[-1] < rsi_series[-3]  # RSI bajó
            bearish_div = price_hh and rsi_lh

        # Volumen en distribución: velas bajistas con más volumen que alcistas
        bear_vol = sum(vols[i] for i in range(-10, 0) if closes[i] < closes[i-1])
        bull_vol = sum(vols[i] for i in range(-10, 0) if closes[i] > closes[i-1])
        vol_dist = bear_vol > bull_vol * 1.2   # 20% más volumen bajista

        # Confirmación 4h
        k4h = utils.get_klines(symbol, interval='4h', limit=30, futures=True)
        c4h  = [float(k[4]) for k in k4h]
        h4h  = [float(k[2]) for k in k4h]
        l4h  = [float(k[3]) for k in k4h]
        v4h  = [float(k[5]) for k in k4h]
        e20_4h   = utils.ema(c4h, 20)[-1]
        e50_4h   = utils.ema(c4h, 50)[-1] if len(c4h) >= 50 else e20_4h
        atr_4h   = utils.atr(h4h, l4h, 14)
        rsi_4h   = utils.rsi(c4h)
        mh_4h    = utils.macd_hist(c4h)
        trend_ok = last < e20_4h   # precio bajo EMA20 en 4h

        # Filtro 15m (momentum bajista de corto plazo)
        ok_15m, reason_15m = confirm_15m(symbol, 'short', futures=True)

        sc = 0
        reasons = []

        # Precio vs EMAs (mayor peso)
        if last < e20:    sc += 2; reasons.append('precio<EMA20_1h')
        if last < e50:    sc += 1; reasons.append('precio<EMA50_1h')
        if last < e20_4h: sc += 1; reasons.append('precio<EMA20_4h')
        if last < e50_4h: sc += 1; reasons.append('precio<EMA50_4h')

        # RSI
        if rsi_v > 70:    sc += 3; reasons.append(f'RSI={rsi_v:.0f} sobrecomprado!')
        elif config.RSI_MIN_SHORT < rsi_v <= 70:
            sc += 1; reasons.append(f'RSI={rsi_v:.0f}')
        if rsi_4h > 60:   sc += 1; reasons.append(f'RSI_4h={rsi_4h:.0f} elevado')

        # MACD
        if mh < 0:    sc += 1; reasons.append('MACD_1h-')
        if mh_4h < 0: sc += 1; reasons.append('MACD_4h-')

        # Setups especiales
        if death_cross:  sc += 2; reasons.append('cruce_de_la_muerte!')
        if bearish_div:  sc += 2; reasons.append('div_bajista!')
        if vol_dist:     sc += 1; reasons.append('vol_distribución')
        if trend_ok:     sc += 1; reasons.append('tendencia_4h bajista')
        if ok_15m:       sc += 2; reasons.append('15m ok')
        else:            reasons.append('15m ❌')

        atr_pct    = (atr_v / last) * 100
        atr_4h_pct = (atr_4h / last) * 100
        min_score  = config.SCORE_MIN_VOLATILE if atr_4h_pct > config.ATR_VOLATILE_THRESH else config.SCORE_MIN

        # Si el 15m no confirma, subir el umbral mínimo
        if not ok_15m:
            min_score = max(min_score, config.SCORE_MIN + 2)

        # SL arriba del precio, TP abajo
        real_sl = utils.round_tick(last + config.SL_ATR_MULT * atr_v, 0.00001)
        real_tp = utils.round_tick(last - config.TP_ATR_MULT * atr_v, 0.00001)
        real_tp = max(real_tp, last * 0.001)

        sl_dist_pct = (real_sl - last) / last * 100
        if sl_dist_pct < config.SL_MIN_DIST_PCT:
            real_sl = utils.round_tick(last * (1 + config.SL_MIN_DIST_PCT / 100), 0.00001)

        return {
            'symbol':    symbol,
            'direction': 'short',
            'score':     sc,
            'min_score': min_score,
            'price':     last,
            'sl':        real_sl,
            'tp':        real_tp,
            'rsi':       rsi_v,
            'rsi_4h':    rsi_4h,
            'atr':       atr_v,
            'atr_pct':   atr_pct,
            'trend_4h':  trend_ok,
            'ok_15m':      ok_15m,
            'death_cross': death_cross,
            'bearish_div': bearish_div,
            'corr_btc':  0.0,
            'reasons':   reasons,
        }
    except Exception as e:
        return None


def scan_longs(btc_ctx, excluded_symbols=None):
    """
    Escanea candidatos long (lista dinámica por volumen + fallback estático).
    Retorna el mejor candidato o None.
    """
    excluded = set(excluded_symbols or [])
    results  = []
    descarte = {}

    if btc_ctx.get('force_mode') == 'short_only':
        descarte['MERCADO'] = 'Caída fuerte de BTC — solo shorts'
        return None, descarte

    # Lista dinámica: filtrar solo los que tienen par en spot
    dyn = get_dynamic_candidates(40)
    # Para longs usamos spot: solo los que existen en spot (intentamos precio)
    candidates = dyn if dyn else _STATIC_CANDIDATES

    for sym in candidates:
        if sym in excluded:
            descarte[sym] = 'ya en posición/cooldown'
            continue

        r = score_long(sym, btc_ctx)
        if r is None:
            descarte[sym] = 'error al obtener datos'
            continue

        if r['atr_pct'] < config.ATR_MIN_PCT:
            descarte[sym] = f'ATR bajo ({r["atr_pct"]:.2f}%)'
            continue
        if r['rsi'] > config.RSI_MAX_LONG:
            descarte[sym] = f'RSI alto ({r["rsi"]:.0f})'
            continue
        if r['corr_btc'] > config.BTC_CORR_MAX and btc_ctx['change_4h'] < config.BTC_WEAK_PCT:
            descarte[sym] = f'Alta corr BTC ({r["corr_btc"]:.2f})'
            continue
        if r['score'] < r['min_score']:
            descarte[sym] = f'score insuf. {r["score"]}/{r["min_score"]}'
            continue

        results.append(r)

    if not results:
        return None, descarte

    best = max(results, key=lambda x: (x['score'], x['atr_pct']))
    return best, descarte


def scan_shorts(btc_ctx, excluded_symbols=None):
    """
    Escanea candidatos short (lista dinámica por volumen + fallback estático).
    Retorna el mejor candidato o None.
    """
    excluded = set(excluded_symbols or [])
    results  = []
    descarte = {}

    if btc_ctx.get('force_mode') == 'long_only':
        descarte['MERCADO'] = 'Subida fuerte de BTC — solo longs'
        return None, descarte

    candidates = get_dynamic_candidates(40)
    if not candidates:
        candidates = _STATIC_CANDIDATES

    for sym in candidates:
        if sym in excluded:
            descarte[sym] = 'ya en posición/cooldown'
            continue

        r = score_short(sym, btc_ctx)
        if r is None:
            descarte[sym] = 'error al obtener datos'
            continue

        if r['atr_pct'] < config.ATR_MIN_PCT:
            descarte[sym] = f'ATR bajo ({r["atr_pct"]:.2f}%)'
            continue
        if r['rsi'] < config.RSI_MIN_SHORT:
            descarte[sym] = f'RSI bajo ({r["rsi"]:.0f})'
            continue
        if r['score'] < r['min_score']:
            descarte[sym] = f'score insuf. {r["score"]}/{r["min_score"]}'
            continue

        results.append(r)

    if not results:
        return None, descarte

    best = max(results, key=lambda x: (x['score'], x['atr_pct']))
    return best, descarte
