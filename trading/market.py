#!/usr/bin/env python3
"""
Análisis de mercado: contexto macro BTC, scoring de candidatos long y short.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import utils, config, binance_client

BINANCE = binance_client.get_default_client()

# Fallback estático si la lista dinámica falla
_STATIC_CANDIDATES = [
    'SOLUSDT', 'BNBUSDT', 'ETHUSDT', 'ADAUSDT', 'DOTUSDT',
    'AVAXUSDT', 'LINKUSDT', 'NEARUSDT', 'MATICUSDT', 'LTCUSDT',
    'ATOMUSDT', 'UNIUSDT', 'AAVEUSDT', 'FTMUSDT', 'INJUSDT',
    'OPUSDT', 'ARBUSDT', 'STXUSDT', 'TIAUSDT', 'WLDUSDT',
    'SNXUSDT', 'LDOUSDT', 'SUIUSDT', 'APTUSDT', 'SEIUSDT',
]

LAST_DECISION_CANDIDATES = {'long': [], 'short': []}

_IMPORTANT_REJECT_MARKERS = (
    'MERCADO',
    'blacklist',
    'auto-blacklist',
    'ATR bajo',
    'ATR alto',
    'RSI alto',
    'RSI bajo',
    'Alta corr BTC',
    'modo direccional',
    'score insuf',
)


def get_last_decision_candidates():
    return {
        'long': list(LAST_DECISION_CANDIDATES.get('long', [])),
        'short': list(LAST_DECISION_CANDIDATES.get('short', [])),
    }


def reset_decision_candidates():
    LAST_DECISION_CANDIDATES['long'] = []
    LAST_DECISION_CANDIDATES['short'] = []


def _decision_record(candidate, side, decision, reason=None):
    c = candidate or {}
    return {
        'symbol': c.get('symbol'),
        'side': side.upper(),
        'score': c.get('score'),
        'decision': decision,
        'reason': reason,
        'rsi': c.get('rsi'),
        'atr': c.get('atr'),
        'atr_pct': c.get('atr_pct'),
        'ema20': c.get('ema20'),
        'ema50': c.get('ema50'),
        'macd_hist': c.get('macd_hist'),
        'volume_ratio': c.get('volume_ratio'),
        'btc_correlation': c.get('btc_correlation', c.get('corr_btc')),
    }


def _symbol_decision_record(symbol, side, decision, reason):
    return _decision_record({'symbol': symbol}, side, decision, reason)


def _is_important_reject(reason):
    text = str(reason or '')
    return any(marker in text for marker in _IMPORTANT_REJECT_MARKERS)


def _is_close_reject(candidate):
    try:
        return candidate.get('decision') == 'rejected' and candidate.get('_margin_to_accept') is not None and candidate['_margin_to_accept'] <= 2
    except Exception:
        return False


def _limit_decisions(records):
    accepted = [r for r in records if r.get('decision') == 'accepted']
    close_rejects = [r for r in records if _is_close_reject(r)]
    important = [
        r for r in records
        if r.get('symbol') == 'MERCADO' or _is_important_reject(r.get('reason'))
    ]
    scored = [r for r in records if r.get('score') is not None]
    top_scored = sorted(scored, key=lambda r: r.get('score') or -9999, reverse=True)[:20]

    selected = []
    seen = set()
    for group in (accepted, close_rejects, top_scored, important):
        for rec in group:
            key = (rec.get('side'), rec.get('symbol'), rec.get('decision'), rec.get('reason'))
            if key in seen:
                continue
            seen.add(key)
            public = {k: v for k, v in rec.items() if not k.startswith('_')}
            selected.append(public)
            if len(selected) >= 40:
                return selected
    return selected


def _store_decisions(side, records):
    LAST_DECISION_CANDIDATES[side] = _limit_decisions(records)

def _check_oversold_for_short(closes, highs, lows):
    """
    Detecta si el precio lleva una caída sostenida antes de entrar short.
    Evita vender en el piso de un dump — alto riesgo de rebote y SL.
    Mirror del filtro anti-momentum-agotado para longs.
    
    Lógica:
    - Si el precio bajó >RECOVERY_FROM_LOW_PCT% desde el máximo de las últimas 24 velas
      Y las últimas N velas son consecutivamente bajistas → penalizar fuerte.
    - Si solo la caída supera el umbral (sin velas consecutivas) → penalizar suave.
    """
    try:
        n = min(24, len(closes))
        recent = closes[-n:]
        high_price = max(recent)
        current   = closes[-1]

        drop_pct = (high_price - current) / high_price * 100
        if drop_pct < config.RECOVERY_FROM_LOW_PCT:
            return 0  # Caída modesta, no penalizar

        # Contar velas 1h consecutivamente bajistas desde el final
        consec = 0
        for i in range(-1, -len(closes), -1):
            if closes[i] < closes[i-1]:
                consec += 1
            else:
                break

        if consec >= config.RECOVERY_CONSEC_CANDLES:
            return 3  # Caída extendida con velas bajistas consecutivas → penalizar fuerte
        else:
            return 2  # Solo caída de precio → penalizar moderado
    except Exception:
        return 0


def _check_recovery_from_low(closes, highs, lows):
    """
    Detecta si el precio lleva un rebote sostenido desde un mínimo reciente.
    Retorna penalización de score (0, 2 o 3) para shorts.
    
    Lógica:
    - Si el precio rebotó >RECOVERY_FROM_LOW_PCT% desde el mínimo de las últimas 24 velas
      Y las últimas N velas 1h son consecutivamente alcistas → penalizar fuertemente.
    - Si solo se cumple el rebote de precio (sin velas consecutivas) → penalizar suave.
    """
    try:
        n = min(24, len(closes))
        recent = closes[-n:]
        low_idx = recent.index(min(recent))
        low_price = recent[low_idx]
        current   = closes[-1]

        # Rebote desde mínimo
        recovery_pct = (current - low_price) / low_price * 100

        if recovery_pct < config.RECOVERY_FROM_LOW_PCT:
            return 0  # Sin rebote significativo, no penalizar

        # Contar velas 1h consecutivamente alcistas desde el final
        consec = 0
        for i in range(-1, -len(closes), -1):
            if closes[i] > closes[i-1]:
                consec += 1
            else:
                break

        if consec >= config.RECOVERY_CONSEC_CANDLES:
            return 3  # Rebote sostenido con velas alcistas consecutivas → penalizar fuerte
        else:
            return 2  # Solo rebote de precio → penalizar moderado
    except Exception:
        return 0


def _check_overbought_for_long(closes):
    """
    Detecta si el precio lleva un impulso alcista sobreextendido antes de entrar long.
    Evita comprar en el techo de un rally — patrón que causó el SL de FET (10:57 UTC 03-Jun).
    
    Lógica:
    - Si el precio subió >RECOVERY_FROM_LOW_PCT% desde el mínimo de las últimas 24 velas
      Y las últimas N velas son consecutivamente alcistas → penalizar fuerte.
    - Si solo el rebote supera el umbral (sin velas consecutivas) → penalizar suave.
    """
    try:
        n = min(24, len(closes))
        recent = closes[-n:]
        low_price = min(recent)
        current   = closes[-1]

        rally_pct = (current - low_price) / low_price * 100
        if rally_pct < config.RECOVERY_FROM_LOW_PCT:
            return 0  # Rally modesto, no penalizar

        # Contar velas 1h consecutivamente alcistas desde el final
        consec = 0
        for i in range(-1, -len(closes), -1):
            if closes[i] > closes[i-1]:
                consec += 1
            else:
                break

        if consec >= config.RECOVERY_CONSEC_CANDLES:
            return 3  # Rally extendido con velas alcistas consecutivas → penalizar fuerte
        else:
            return 2  # Solo rally de precio → penalizar moderado
    except Exception:
        return 0


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
        tickers = BINANCE.fut_public('/fapi/v1/ticker/24hr')
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
        k15 = BINANCE.get_klines(symbol, interval='15m', limit=20, futures=futures)
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
        k4h = BINANCE.get_klines('BTCUSDT', interval='4h', limit=52)
        closes_4h = [float(k[4]) for k in k4h]
        highs_4h  = [float(k[2]) for k in k4h]
        lows_4h   = [float(k[3]) for k in k4h]

        k1h = BINANCE.get_klines('BTCUSDT', interval='1h', limit=2)
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


def check_btc_momentum(btc_ctx):
    """
    Verifica si el momentum de BTC excede el umbral para pausar nuevas entradas.
    Retorna (pause_longs, pause_shorts, reason) donde:
      - Si |BTC| >umbral: pausa AMBOS lados (alto riesgo de reversa)
      - reason explica por qué
    """
    change_4h = btc_ctx.get('change_4h', 0)
    threshold = config.BTC_MOMENTUM_PAUSE_PCT
    window = config.BTC_MOMENTUM_WINDOW_H
    
    if change_4h >= threshold:
        # BTC subió mucho -> pausar ambos (shorts sufren pump, longs compran top)
        return True, True, f'BTC +{change_4h:.1f}% en {window}h — pausa entradas (pump extremo)'
    elif change_4h <= -threshold:
        # BTC bajó mucho -> pausar ambos (longs sufren dump, shorts venden piso)
        return True, True, f'BTC {change_4h:.1f}% en {window}h — pausa entradas (dump extremo)'
    
    return False, False, None


def check_btc_momentum_close(btc_ctx):
    """
    Verifica si el momentum de BTC es tan extremo que justifica cerrar posiciones existentes.
    Retorna (close_shorts, close_longs, reason) donde:
      - close_shorts=True si BTC subió >4% (pump extremo → shorts en peligro)
      - close_longs=True si BTC bajó >4% (dump extremo → longs en peligro)
      - reason explica por qué
    """
    change_4h = btc_ctx.get('change_4h', 0)
    pump_threshold = config.BTC_MOMENTUM_CLOSE_PCT
    dump_threshold = config.BTC_MOMENTUM_CLOSE_LONGS
    window = config.BTC_MOMENTUM_WINDOW_H
    
    if change_4h >= pump_threshold:
        # Pump extremo → cerrar SHORTS (van a tocar SL)
        return True, False, f'BTC +{change_4h:.1f}% en {window}h — cierre preventivo de SHORTS'
    elif change_4h <= dump_threshold:
        # Dump extremo → cerrar LONGS (van a tocar SL)
        return False, True, f'BTC {change_4h:.1f}% en {window}h — cierre preventivo de LONGS'
    
    return False, False, None


def score_long(symbol, btc_ctx):
    """
    Evalúa un símbolo como candidato LONG (spot).
    Timeframes: 15m (momentum) + 1h (entrada) + 4h (tendencia).
    """
    try:
        # Filtro apertura mercado US: no entrar en stocks tokenizados
        # durante los primeros N minutos de apertura (alta volatilidad / price discovery)
        import time as _time
        if symbol in config.US_STOCK_TOKENS:
            _now = _time.gmtime()
            open_h, open_m = config.US_MARKET_OPEN_UTC
            minutes_since_open = (_now.tm_hour - open_h) * 60 + (_now.tm_min - open_m)
            if 0 <= minutes_since_open < config.US_MARKET_AVOID_MIN:
                return None  # demasiado cerca de la apertura US — descartar

        # Score dinámico por día/hora (basado en histórico de SLs)
        now = _time.gmtime()
        weekday = now.tm_wday  # 0=lunes
        hour = now.tm_hour
        
        hour_penalty = 0
        hour_reason = ''
        # Lunes: +2 (66.7% SL rate histórico)
        if weekday == 0:
            hour_penalty += 2
            hour_reason = 'lunes+2'
        # Martes: +1 (58.3% SL rate)
        elif weekday == 1:
            hour_penalty += 1
            hour_reason = 'martes+1'
        # 06:00-12:00 UTC: +1 (58.3% SL rate)
        if 6 <= hour < 12:
            hour_penalty += 1
            hour_reason += '+horario_eu'
        # 00:00-04:00 UTC: +1 (horas más muertas)
        if 0 <= hour < 4:
            hour_penalty += 1
            hour_reason += '+night'
        # 18:00-24:00 UTC: -1 (mejor slot, 28.6% SL rate)
        if 18 <= hour < 24:
            hour_penalty = max(0, hour_penalty - 1)
            if hour_reason:
                hour_reason += '-bonus_us'
            else:
                hour_reason = 'bonus_us-1'

        k1h = BINANCE.get_klines(symbol, interval='1h', limit=50)
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
        k4h      = BINANCE.get_klines(symbol, interval='4h', limit=24)
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

        # Score dinámico por día/hora (long)
        if hour_penalty > 0:
            min_score = min_score + hour_penalty
            reasons.append(f'ajuste_día/hora ({hour_reason}) → score+{hour_penalty}')
        elif hour_penalty < 0:
            min_score = max(config.SCORE_MIN, min_score - 1)
            reasons.append(f'bonus_día/hora ({hour_reason}) → score-1')

        # Si el 15m falla explícitamente, subir el min_score requerido
        if not ok_15m:
            min_score = max(min_score, config.SCORE_MIN + 2)

        # Filtro volumen relativo: no entrar si volumen actual < 50% del promedio 24h
        # Evita entrar en horas muertas o monedas sin liquidez (slippage, manipulación)
        vol_24h_avg = sum(vols[-24:]) / 24 if len(vols) >= 24 else sum(vols) / len(vols)
        vol_ratio = vols[-1] / vol_24h_avg if vol_24h_avg > 0 else 1.0
        if vol_ratio < 0.5:
            return None  # Volumen demasiado bajo — descartar candidato

        # Filtro anti-momentum-agotado para longs: penalizar si el precio subió demasiado
        overbought_penalty = _check_overbought_for_long(closes)
        if overbought_penalty > 0:
            min_score = min_score + overbought_penalty
            reasons.append(f'impulso_sobreextendido → score+{overbought_penalty} requerido')

        # ATR expansion filter: si ATR actual > 2× ATR promedio 7 días → volatilidad anormal
        atr_7d_avg = sum(atr_v / max(closes[i], 0.0001) * 100 for i in range(-7, 0)) / 7 if len(closes) >= 7 else atr_pct
        if atr_pct > atr_7d_avg * 2:
            min_score = min_score + 3
            reasons.append(f'ATR expansión ({atr_pct:.1f}% vs {atr_7d_avg:.1f}% promedio) → score+3')

        # Distancia a máximos 24h: no entrar long si resistencia está <2% arriba
        high_24h = max(highs[-24:]) if len(highs) >= 24 else max(highs)
        dist_to_high = (high_24h - last) / last * 100
        if dist_to_high < 2.0:
            min_score = min_score + 2
            reasons.append(f'resistencia_cerca ({dist_to_high:.1f}% al high 24h) → score+2')

        sl = round(last - config.SL_ATR_MULT * atr_v, 8)
        tp = round(last + config.TP_ATR_MULT * atr_v, 8)

        sl_dist_pct = (last - sl) / last * 100
        if sl_dist_pct < config.SL_MIN_DIST_PCT:
            sl = round(last * (1 - config.SL_MIN_DIST_PCT / 100), 8)

        # Correlación BTC si BTC está débil
        corr_btc = 0.0
        if btc_ctx['change_4h'] < config.BTC_WEAK_PCT and symbol != 'BTCUSDT':
            try:
                btc_k   = BINANCE.get_klines('BTCUSDT', interval='1h', limit=48)
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
            'ema20':     e20,
            'ema50':     e50,
            'macd_hist': mh,
            'volume_ratio': vol_ratio,
            'trend_4h':  trend_ok,
            'ok_15m':    ok_15m,
            'corr_btc':  corr_btc,
            'btc_correlation': corr_btc,
            'reasons':   reasons,
            'reject_reason': None,
            'reject_reasons': None,
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
        # Filtro apertura mercado US: no entrar en stocks tokenizados
        # durante los primeros N minutos de apertura (alta volatilidad / price discovery)
        import time as _time
        if symbol in config.US_STOCK_TOKENS:
            _now = _time.gmtime()
            open_h, open_m = config.US_MARKET_OPEN_UTC
            minutes_since_open = (_now.tm_hour - open_h) * 60 + (_now.tm_min - open_m)
            if 0 <= minutes_since_open < config.US_MARKET_AVOID_MIN:
                return None  # demasiado cerca de la apertura US — descartar

        # Score dinámico por día/hora (mismo criterio que score_long)
        now = _time.gmtime()
        weekday = now.tm_wday
        hour = now.tm_hour
        
        hour_penalty = 0
        hour_reason = ''
        if weekday == 0:
            hour_penalty += 2
            hour_reason = 'lunes+2'
        elif weekday == 1:
            hour_penalty += 1
            hour_reason = 'martes+1'
        if 6 <= hour < 12:
            hour_penalty += 1
            hour_reason += '+horario_eu'
        if 0 <= hour < 4:
            hour_penalty += 1
            hour_reason += '+night'
        if 18 <= hour < 24:
            hour_penalty = max(0, hour_penalty - 1)
            if hour_reason:
                hour_reason += '-bonus_us'
            else:
                hour_reason = 'bonus_us-1'

        k1h = BINANCE.get_klines(symbol, interval='1h', limit=60, futures=True)
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
        # Fix #5: en lugar de recalcular RSI desde cero para cada slice (O(n²)),
        # comparamos RSI en 3 puntos clave usando la serie de closes completa.
        rsi_now  = utils.rsi(closes)                     # RSI sobre closes completos
        rsi_mid  = utils.rsi(closes[:-10])               # RSI hace 10 velas
        rsi_old  = utils.rsi(closes[:-20])               # RSI hace 20 velas
        bearish_div = (
            closes[-1] > closes[-11]                     # precio: higher high vs hace 10 velas
            and rsi_now < rsi_mid                        # RSI: lower high actual vs hace 10
            and closes[-11] > closes[-21]                # precio también subió en el periodo anterior
            and rsi_mid < rsi_old                        # y RSI también bajó en ese periodo
        )

        # Volumen en distribución: velas bajistas con más volumen que alcistas
        bear_vol = sum(vols[i] for i in range(-10, 0) if closes[i] < closes[i-1])
        bull_vol = sum(vols[i] for i in range(-10, 0) if closes[i] > closes[i-1])
        vol_dist = bear_vol > bull_vol * 1.2   # 20% más volumen bajista

        # Confirmación 4h
        k4h = BINANCE.get_klines(symbol, interval='4h', limit=30, futures=True)
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

        # Score dinámico por día/hora (short)
        if hour_penalty > 0:
            min_score = min_score + hour_penalty
            reasons.append(f'ajuste_día/hora ({hour_reason}) → score+{hour_penalty}')
        elif hour_penalty < 0:
            min_score = max(config.SCORE_MIN, min_score - 1)
            reasons.append(f'bonus_día/hora ({hour_reason}) → score-1')

        # Si el 15m no confirma, subir el umbral mínimo
        if not ok_15m:
            min_score = max(min_score, config.SCORE_MIN + 2)

        # Filtro volumen relativo: no entrar si volumen actual < 50% del promedio 24h
        vol_24h_avg = sum(vols[-24:]) / 24 if len(vols) >= 24 else sum(vols) / len(vols)
        vol_ratio = vols[-1] / vol_24h_avg if vol_24h_avg > 0 else 1.0
        if vol_ratio < 0.5:
            return None  # Volumen demasiado bajo — descartar candidato

        # Penalizar si BTC está en rebote 1h (arrastra altcoins al alza → shorts peligrosos)
        btc_rebound = btc_ctx.get('change_1h', 0) > config.BTC_REBOUND_1H_PCT
        if btc_rebound:
            min_score = min_score + 2
            reasons.append(f'BTC rebote 1h ({btc_ctx.get("change_1h", 0):+.2f}%) → score+2 requerido')

        # Filtro de recuperación desde mínimo: evitar shorts en rebote sostenido
        recovery_penalty = _check_recovery_from_low(closes, highs, lows)
        if recovery_penalty > 0:
            min_score = min_score + recovery_penalty
            reasons.append(f'rebote_desde_mínimo → score+{recovery_penalty} requerido')

        # Filtro anti-piso: evitar shorts si el precio viene cayendo mucho (riesgo rebote)
        oversold_penalty = _check_oversold_for_short(closes, highs, lows)
        if oversold_penalty > 0:
            min_score = min_score + oversold_penalty
            reasons.append(f'caída_sostenida → score+{oversold_penalty} requerido')

        # ATR expansion filter: si ATR actual > 2× ATR promedio 7 días → volatilidad anormal
        atr_7d_avg = sum(atr_v / max(closes[i], 0.0001) * 100 for i in range(-7, 0)) / 7 if len(closes) >= 7 else atr_pct
        if atr_pct > atr_7d_avg * 2:
            min_score = min_score + 3
            reasons.append(f'ATR expansión ({atr_pct:.1f}% vs {atr_7d_avg:.1f}% promedio) → score+3')

        # Distancia a mínimos 24h: no entrar short si soporte está <2% abajo
        low_24h = min(lows[-24:]) if len(lows) >= 24 else min(lows)
        dist_to_low = (last - low_24h) / last * 100
        if dist_to_low < 2.0:
            min_score = min_score + 2
            reasons.append(f'soporte_cerca ({dist_to_low:.1f}% al low 24h) → score+2')

        # SL arriba del precio, TP abajo
        real_sl = utils.round_tick(last + config.SL_ATR_MULT_SHORT * atr_v, 0.00001)
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
            'ema20':     e20,
            'ema50':     e50,
            'macd_hist': mh,
            'volume_ratio': vol_ratio,
            'trend_4h':  trend_ok,
            'ok_15m':      ok_15m,
            'death_cross': death_cross,
            'bearish_div': bearish_div,
            'corr_btc':  0.0,
            'btc_correlation': 0.0,
            'reasons':   reasons,
            'reject_reason': None,
            'reject_reasons': None,
        }
    except Exception as e:
        return None



# ── Blacklist dinámica (persiste entre reinicios) ─────────────────────────────
import os as _os
_DYNAMIC_BL_FILE = _os.path.join(_os.path.dirname(__file__), 'blacklist_dynamic.json')

def _load_dynamic_blacklist():
    """Carga la blacklist dinámica desde disco y la fusiona con la de config."""
    try:
        with open(_DYNAMIC_BL_FILE, encoding='utf-8') as f:
            data = __import__('json').load(f)
        for sym in data.get('symbols', []):
            config.BLACKLIST_SYMBOLS.add(sym)
    except FileNotFoundError:
        pass
    except Exception:
        pass

def _persist_blacklist(sym, reason):
    """Persiste un nuevo símbolo en la blacklist dinámica y manda alerta (una sola vez)."""
    import json as _json
    try:
        try:
            with open(_DYNAMIC_BL_FILE, encoding='utf-8') as f:
                data = _json.load(f)
        except FileNotFoundError:
            data = {'symbols': [], 'log': []}
        if sym not in data['symbols']:
            data['symbols'].append(sym)
            data['log'].append({'symbol': sym, 'reason': reason,
                                'added': __import__('time').strftime('%Y-%m-%d %H:%M UTC')})
            with open(_DYNAMIC_BL_FILE, 'w', encoding='utf-8') as f:
                _json.dump(data, f, indent=2)
            utils.send_alert(f'⚠️ {sym} agregado a blacklist: {reason}')
    except Exception:
        pass

def _remove_from_dynamic_blacklist(sym, reason):
    """Elimina un símbolo de la blacklist dinámica y notifica."""
    import json as _json
    try:
        with open(_DYNAMIC_BL_FILE, encoding='utf-8') as f:
            data = _json.load(f)
        if sym in data.get('symbols', []):
            data['symbols'].remove(sym)
            data.setdefault('rehabilitated', []).append({
                'symbol': sym, 'reason': reason,
                'date': __import__('time').strftime('%Y-%m-%d %H:%M UTC')
            })
            with open(_DYNAMIC_BL_FILE, 'w', encoding='utf-8') as f:
                _json.dump(data, f, indent=2)
        config.BLACKLIST_SYMBOLS.discard(sym)
        utils.send_alert(f'✅ {sym} rehabilitado desde blacklist: {reason}')
    except Exception:
        pass


def review_dynamic_blacklist():
    """
    Revisa la blacklist dinámica y rehabilita tokens cuya volatilidad
    ya está dentro de parámetros normales y llevan al menos MIN_DAYS_BLACKLISTED días.

    Criterios de salida (todos deben cumplirse):
      1. ≥48h desde que fue agregado
      2. Volatilidad horaria actual < RISKY_VOL_HOURLY_MAX / 2  (umbrales relajados)
      3. Rango 48h actual < RISKY_RANGE_48H_MAX / 2
      4. Si entró por SLs recurrentes: el sl_history ya no tiene ≥2 SLs en los últimos 5 días
         (ese chequeo lo hace el llamador pasando sl_history)

    Retorna lista de símbolos rehabilitados.
    """
    import json as _json, time as _time, math
    rehabilitated = []
    try:
        with open(_DYNAMIC_BL_FILE, encoding='utf-8') as f:
            data = _json.load(f)
    except FileNotFoundError:
        return []

    MIN_HOURS_BLACKLISTED = 48

    for entry in data.get('log', []):
        sym    = entry.get('symbol', '')
        reason = entry.get('reason', '')
        added  = entry.get('added', '')

        # Solo procesar tokens que sigan en la blacklist activa
        if sym not in data.get('symbols', []):
            continue

        # Verificar tiempo mínimo en blacklist
        try:
            import time as _t
            added_ts = _t.mktime(_t.strptime(added, '%Y-%m-%d %H:%M UTC'))
            hours_in_bl = (_t.time() - added_ts) / 3600
        except Exception:
            continue
        if hours_in_bl < MIN_HOURS_BLACKLISTED:
            continue

        # Medir volatilidad actual
        try:
            futures = True  # todos los de la blacklist dinámica son futures
            k = BINANCE.get_klines(sym, interval='1h', limit=48, futures=futures)
            closes = [float(x[4]) for x in k]
            highs  = [float(x[2]) for x in k]
            lows   = [float(x[3]) for x in k]
        except Exception:
            continue

        returns   = [(closes[i]-closes[i-1])/closes[i-1]*100 for i in range(1, len(closes))]
        mean_r    = sum(returns) / len(returns)
        std_r     = math.sqrt(sum((r-mean_r)**2 for r in returns) / len(returns))
        p_min     = min(closes); p_max = max(closes)
        range_48h = (p_max - p_min) / p_min * 100

        vol_ok   = std_r   < config.RISKY_VOL_HOURLY_MAX / 2   # mitad del umbral de entrada
        range_ok = range_48h < config.RISKY_RANGE_48H_MAX / 2

        if vol_ok and range_ok:
            rehab_reason = (f'vol actual {std_r:.1f}%/h y rango48h {range_48h:.0f}% '
                           f'dentro de parámetros ({hours_in_bl:.0f}h en blacklist)')
            _remove_from_dynamic_blacklist(sym, rehab_reason)
            rehabilitated.append(sym)

    return rehabilitated


# Cargar blacklist dinámica al importar el módulo
_load_dynamic_blacklist()


def check_volatility_risk(symbol, futures=False):
    """
    Chequea si un token es demasiado volátil para operar.
    Retorna (is_risky: bool, auto_blacklist: bool, reason: str)
      - is_risky: aplicar filtros extra (RISKY_SYMBOLS)
      - auto_blacklist: demasiado peligroso, descartar siempre
    """
    try:
        import math
        k1h = BINANCE.get_klines(symbol, interval='1h', limit=48, futures=futures)
        closes = [float(k[4]) for k in k1h]
        highs  = [float(k[2]) for k in k1h]
        lows   = [float(k[3]) for k in k1h]

        # Volatilidad horaria (desv. estándar de retornos %)
        returns = [(closes[i]-closes[i-1])/closes[i-1]*100 for i in range(1, len(closes))]
        mean_r = sum(returns) / len(returns)
        std_r  = math.sqrt(sum((r - mean_r)**2 for r in returns) / len(returns))

        # Rango 48h
        p_min = min(closes); p_max = max(closes)
        range_48h = (p_max - p_min) / p_min * 100

        # Auto-blacklist si supera umbrales duros
        if std_r > config.RISKY_VOL_HOURLY_MAX:
            return True, True, f'volatilidad extrema {std_r:.1f}%/h'
        if range_48h > config.RISKY_RANGE_48H_MAX:
            return True, True, f'rango 48h {range_48h:.0f}%'

        # Risky si supera la mitad del umbral
        is_risky = std_r > config.RISKY_VOL_HOURLY_MAX / 2 or range_48h > config.RISKY_RANGE_48H_MAX / 2
        reason = f'vol={std_r:.1f}%/h range48h={range_48h:.0f}%'
        return is_risky, False, reason
    except Exception:
        return False, False, ''

def scan_longs(btc_ctx, excluded_symbols=None):
    """
    Escanea candidatos long (lista dinámica por volumen + fallback estático).
    Retorna el mejor candidato o None.
    
    ORDEN DE FILTROS:
    1. Momentum BTC (prioridad máxima) - si BTC se movió >2%, pausa todo
    2. Modo direccional - si es bearish, bloquea longs
    """
    decision_records = []
    # ── FILTRO 1: Momentum BTC (prioridad sobre modo direccional) ─────────────
    # Si BTC se movió >2% en 4h, NO abrir nada (alto riesgo de reversa)
    pause_longs, pause_shorts, reason = check_btc_momentum(btc_ctx)
    if pause_longs:
        _store_decisions('long', [_symbol_decision_record('MERCADO', 'LONG', 'skipped', reason)])
        return None, {'MERCADO': reason}
    
    # ── FILTRO 2: Modo direccional ────────────────────────────────────────────
    if config.DIRECTIONAL_MODE:
        trend = btc_ctx.get('trend', 'neutral')
        if trend == 'bearish':
            _store_decisions('long', [_symbol_decision_record('MERCADO', 'LONG', 'skipped', 'modo direccional: longs bloqueados en bearish')])
            return None, {'MERCADO': 'modo direccional: longs bloqueados en bearish'}
        # En neutral, permitir solo si DIRECTIONAL_NEUTRAL_BOTH=True
        if trend == 'neutral' and not config.DIRECTIONAL_NEUTRAL_BOTH:
            _store_decisions('long', [_symbol_decision_record('MERCADO', 'LONG', 'skipped', 'modo direccional: longs bloqueados en neutral')])
            return None, {'MERCADO': 'modo direccional: longs bloqueados en neutral'}
    
    excluded = set(excluded_symbols or [])
    results  = []
    descarte = {}

    if btc_ctx.get('force_mode') == 'short_only':
        descarte['MERCADO'] = 'Caída fuerte de BTC — solo shorts'
        _store_decisions('long', [_symbol_decision_record('MERCADO', 'LONG', 'skipped', descarte['MERCADO'])])
        return None, descarte

    # Contexto bajista: subir el umbral de score para filtrar oportunidades mediocres
    counter_trend = btc_ctx.get('trend') == 'bearish'

    # Lista dinámica: filtrar solo los que tienen par en spot
    dyn = get_dynamic_candidates(40)
    # Para longs usamos spot: solo los que existen en spot (intentamos precio)
    candidates = dyn if dyn else _STATIC_CANDIDATES

    for sym in candidates:
        if sym in excluded:
            descarte[sym] = 'ya en posición/cooldown'
            decision_records.append(_symbol_decision_record(sym, 'LONG', 'skipped', descarte[sym]))
            continue

        if sym in config.BLACKLIST_SYMBOLS:
            descarte[sym] = 'blacklist (microcap/riesgo)'
            decision_records.append(_symbol_decision_record(sym, 'LONG', 'rejected', descarte[sym]))
            continue

        # Check de volatilidad: auto-blacklist si extrema, filtros extra si risky
        _is_risky, _auto_bl, _vol_reason = check_volatility_risk(sym, futures=False)
        if _auto_bl:
            descarte[sym] = f'auto-blacklist: {_vol_reason}'
            decision_records.append(_symbol_decision_record(sym, 'LONG', 'rejected', descarte[sym]))
            if sym not in config.BLACKLIST_SYMBOLS:
                config.BLACKLIST_SYMBOLS.add(sym)
                _persist_blacklist(sym, _vol_reason)
            continue

        r = score_long(sym, btc_ctx)
        if r is None:
            descarte[sym] = 'error al obtener datos'
            decision_records.append(_symbol_decision_record(sym, 'LONG', 'skipped', descarte[sym]))
            continue
        # Si es risky, aplicar score bonus y reducir capital
        if _is_risky or sym in config.RISKY_SYMBOLS:
            r['min_score'] = r.get('min_score', config.SCORE_MIN) + config.RISKY_SCORE_BONUS
            r['risky'] = True
            r['vol_reason'] = _vol_reason
        if r is None:
            descarte[sym] = 'error al obtener datos'
            decision_records.append(_symbol_decision_record(sym, 'LONG', 'skipped', descarte[sym]))
            continue

        if r['atr_pct'] < config.ATR_MIN_PCT:
            descarte[sym] = f'ATR bajo ({r["atr_pct"]:.2f}%)'
            r['reject_reason'] = descarte[sym]
            r['reject_reasons'] = [descarte[sym]]
            decision_records.append(_decision_record(r, 'LONG', 'rejected', descarte[sym]))
            continue
        if r['atr_pct'] > config.ATR_MAX_PCT:
            descarte[sym] = f'ATR alto ({r["atr_pct"]:.2f}%)'
            r['reject_reason'] = descarte[sym]
            r['reject_reasons'] = [descarte[sym]]
            decision_records.append(_decision_record(r, 'LONG', 'rejected', descarte[sym]))
            continue
        if r['rsi'] > config.RSI_MAX_LONG:
            descarte[sym] = f'RSI alto ({r["rsi"]:.0f})'
            r['reject_reason'] = descarte[sym]
            r['reject_reasons'] = [descarte[sym]]
            decision_records.append(_decision_record(r, 'LONG', 'rejected', descarte[sym]))
            continue
        if r['corr_btc'] > config.BTC_CORR_MAX and btc_ctx['change_4h'] < config.BTC_WEAK_PCT:
            descarte[sym] = f'Alta corr BTC ({r["corr_btc"]:.2f})'
            r['reject_reason'] = descarte[sym]
            r['reject_reasons'] = [descarte[sym]]
            decision_records.append(_decision_record(r, 'LONG', 'rejected', descarte[sym]))
            continue

        # Si el contexto es bajista, exigir score mucho más alto (contra-tendencia)
        effective_min = max(r['min_score'], config.SCORE_MIN_COUNTER) if counter_trend else r['min_score']
        if r['score'] < effective_min:
            tag = ' [ctx bajista]' if counter_trend and r['score'] >= r['min_score'] else ''
            descarte[sym] = f'score insuf. {r["score"]}/{effective_min}{tag}'
            r['reject_reason'] = descarte[sym]
            r['reject_reasons'] = [descarte[sym]]
            rec = _decision_record(r, 'LONG', 'rejected', descarte[sym])
            rec['_margin_to_accept'] = effective_min - r['score']
            decision_records.append(rec)
            continue

        results.append(r)

    if not results:
        _store_decisions('long', decision_records)
        return None, descarte

    best = max(results, key=lambda x: (x['score'], x['atr_pct']))
    for r in results:
        if r is best:
            decision_records.append(_decision_record(r, 'LONG', 'accepted', None))
        else:
            decision_records.append(_decision_record(r, 'LONG', 'skipped', 'not selected'))
    _store_decisions('long', decision_records)
    # Marcar si es contexto bajista para que longs.py ajuste el riesgo
    best['bearish_context'] = counter_trend
    return best, descarte


def scan_shorts(btc_ctx, excluded_symbols=None):
    """
    Escanea candidatos short (lista dinámica por volumen + fallback estático).
    Retorna el mejor candidato o None.
    
    ORDEN DE FILTROS:
    1. Momentum BTC (prioridad máxima) - si BTC se movió >2%, pausa todo
    2. Modo direccional - si es bullish, bloquea shorts
    """
    decision_records = []
    # ── FILTRO 1: Momentum BTC (prioridad sobre modo direccional) ─────────────
    # Si BTC se movió >2% en 4h, NO abrir nada (alto riesgo de reversa)
    pause_longs, pause_shorts, reason = check_btc_momentum(btc_ctx)
    if pause_shorts:
        _store_decisions('short', [_symbol_decision_record('MERCADO', 'SHORT', 'skipped', reason)])
        return None, {'MERCADO': reason}
    
    # ── FILTRO 2: Modo direccional ────────────────────────────────────────────
    if config.DIRECTIONAL_MODE:
        trend = btc_ctx.get('trend', 'neutral')
        if trend == 'bullish':
            _store_decisions('short', [_symbol_decision_record('MERCADO', 'SHORT', 'skipped', 'modo direccional: shorts bloqueados en bullish')])
            return None, {'MERCADO': 'modo direccional: shorts bloqueados en bullish'}
        if trend == 'neutral' and not config.DIRECTIONAL_NEUTRAL_BOTH:
            _store_decisions('short', [_symbol_decision_record('MERCADO', 'SHORT', 'skipped', 'modo direccional: shorts bloqueados en neutral')])
            return None, {'MERCADO': 'modo direccional: shorts bloqueados en neutral'}
    
    excluded = set(excluded_symbols or [])
    results  = []
    descarte = {}

    if btc_ctx.get('force_mode') == 'long_only':
        descarte['MERCADO'] = 'Subida fuerte de BTC — solo longs'
        _store_decisions('short', [_symbol_decision_record('MERCADO', 'SHORT', 'skipped', descarte['MERCADO'])])
        return None, descarte

    # Contexto alcista: subir el umbral de score para filtrar shorts mediocres
    counter_trend = btc_ctx.get('trend') == 'bullish'

    candidates = get_dynamic_candidates(40)
    if not candidates:
        candidates = _STATIC_CANDIDATES

    for sym in candidates:
        if sym in excluded:
            descarte[sym] = 'ya en posición/cooldown'
            decision_records.append(_symbol_decision_record(sym, 'SHORT', 'skipped', descarte[sym]))
            continue

        if sym in config.BLACKLIST_SYMBOLS:
            descarte[sym] = 'blacklist (microcap/riesgo)'
            decision_records.append(_symbol_decision_record(sym, 'SHORT', 'rejected', descarte[sym]))
            continue

        # Check de volatilidad
        _is_risky, _auto_bl, _vol_reason = check_volatility_risk(sym, futures=True)
        if _auto_bl:
            descarte[sym] = f'auto-blacklist: {_vol_reason}'
            decision_records.append(_symbol_decision_record(sym, 'SHORT', 'rejected', descarte[sym]))
            if sym not in config.BLACKLIST_SYMBOLS:
                config.BLACKLIST_SYMBOLS.add(sym)
                _persist_blacklist(sym, _vol_reason)
            continue

        r = score_short(sym, btc_ctx)
        if r is None:
            descarte[sym] = 'error al obtener datos'
            decision_records.append(_symbol_decision_record(sym, 'SHORT', 'skipped', descarte[sym]))
            continue
        if _is_risky or sym in config.RISKY_SYMBOLS:
            r['min_score'] = r.get('min_score', config.SCORE_MIN) + config.RISKY_SCORE_BONUS
            r['risky'] = True
            r['vol_reason'] = _vol_reason
        if r is None:
            descarte[sym] = 'error al obtener datos'
            decision_records.append(_symbol_decision_record(sym, 'SHORT', 'skipped', descarte[sym]))
            continue

        if r['atr_pct'] < config.ATR_MIN_PCT:
            descarte[sym] = f'ATR bajo ({r["atr_pct"]:.2f}%)'
            r['reject_reason'] = descarte[sym]
            r['reject_reasons'] = [descarte[sym]]
            decision_records.append(_decision_record(r, 'SHORT', 'rejected', descarte[sym]))
            continue
        if r['atr_pct'] > config.ATR_MAX_PCT:
            descarte[sym] = f'ATR alto ({r["atr_pct"]:.2f}%)'
            r['reject_reason'] = descarte[sym]
            r['reject_reasons'] = [descarte[sym]]
            decision_records.append(_decision_record(r, 'SHORT', 'rejected', descarte[sym]))
            continue
        if r['rsi'] < config.RSI_MIN_SHORT:
            descarte[sym] = f'RSI bajo ({r["rsi"]:.0f})'
            r['reject_reason'] = descarte[sym]
            r['reject_reasons'] = [descarte[sym]]
            decision_records.append(_decision_record(r, 'SHORT', 'rejected', descarte[sym]))
            continue

        # Si el contexto es alcista, exigir score mucho más alto (contra-tendencia)
        effective_min = max(r['min_score'], config.SCORE_MIN_COUNTER) if counter_trend else r['min_score']
        if r['score'] < effective_min:
            tag = ' [ctx alcista]' if counter_trend and r['score'] >= r['min_score'] else ''
            descarte[sym] = f'score insuf. {r["score"]}/{effective_min}{tag}'
            r['reject_reason'] = descarte[sym]
            r['reject_reasons'] = [descarte[sym]]
            rec = _decision_record(r, 'SHORT', 'rejected', descarte[sym])
            rec['_margin_to_accept'] = effective_min - r['score']
            decision_records.append(rec)
            continue

        results.append(r)

    if not results:
        _store_decisions('short', decision_records)
        return None, descarte

    best = max(results, key=lambda x: (x['score'], x['atr_pct']))
    for r in results:
        if r is best:
            decision_records.append(_decision_record(r, 'SHORT', 'accepted', None))
        else:
            decision_records.append(_decision_record(r, 'SHORT', 'skipped', 'not selected'))
    _store_decisions('short', decision_records)
    return best, descarte
