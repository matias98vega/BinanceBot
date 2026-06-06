#!/usr/bin/env python3
"""
Rebalanceo automático de capital entre Spot y Futures según contexto de mercado.

Lógica:
  - bearish  → futures 65% / spot 35%  (más capital para shorts)
  - bullish  → spot 65% / futures 35%  (más capital para longs)
  - neutral  → spot 50% / futures 50%

Filosofía de cambio de tendencia:
  Cuando el mercado cambia de dirección, NO se fuerza el rebalanceo inmediato.
  Las posiciones abiertas (de la tendencia anterior) se dejan correr hasta su
  cierre natural (TP/SL). El capital que liberan se transfiere progresivamente
  a la wallet correcta. Esto evita sacrificar riesgo por operación en días
  normales para resolver un problema que ocurre una vez cada varios días.

  El rebalanceo "paciente" trabaja sobre el capital LIBRE disponible en cada
  momento. El target se calcula sobre el capital TOTAL (libre + en posiciones)
  para saber hacia dónde ir, pero solo transfiere lo que está disponible ahora.
"""

import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import utils, config, market

# ── Parámetros ────────────────────────────────────────────────────────────────
RATIO_BEARISH_FUTURES      = 0.65   # futures recibe 65% cuando mercado bajista
RATIO_VERY_BEARISH_FUTURES = 0.80   # futures recibe 80% cuando bajista >3 días consecutivos
VERY_BEARISH_DAYS          = 3.0    # umbral de días consecutivos bajistas
RATIO_BULLISH_SPOT         = 0.65   # spot recibe 65% cuando mercado alcista
RATIO_VERY_BULLISH_SPOT    = 0.80   # spot recibe 80% cuando alcista >3 días consecutivos
VERY_BULLISH_DAYS          = 3.0    # umbral de días consecutivos alcistas
REBALANCE_MIN_USDT         = 2.0    # no transferir menos de $2
REBALANCE_MIN_WALLET       = 3.0    # no dejar ninguna wallet con menos de $3 libres


def _capital_total(state):
    """Capital total = libre + comprometido en posiciones (ambas wallets)."""
    import urllib.error, time, logging
    
    spot_free   = utils.get_usdt_spot()
    
    # Futures: walletBalance + uPnL (balance real, igual al resumen diario)
    # Con reintentos ante errores transitorios de API
    account = None
    last_err = None
    for _attempt in range(3):
        try:
            account = utils.fut_signed('GET', '/fapi/v2/account', {})
            break
        except urllib.error.HTTPError as e:
            last_err = e
            if _attempt < 2:
                _delay = 5 * (_attempt + 1)  # 5s, 10s
                logging.warning(f'Rebalance: intento {_attempt+1} fallido ({e}), reintentando en {_delay}s')
                time.sleep(_delay)
    if account is None:
        raise last_err
    
    fut_wallet  = float(account.get('totalWalletBalance', 0))
    fut_upnl    = float(account.get('totalUnrealizedProfit', 0))
    fut_total   = fut_wallet + fut_upnl
    spot_in_pos = sum(
        p['entry_price'] * p['quantity']
        for p in state.get('positions', []) if p['direction'] == 'long'
    )
    # Para rebalanceo, fut_free = disponible para nuevas posiciones
    fut_free    = float(account.get('availableBalance', 0))
    return spot_free, fut_free, spot_in_pos, fut_total


def _trend_changed(state, new_trend):
    """Devuelve True si la tendencia actual es distinta a la del último rebalanceo."""
    last = state.get('last_rebalance_trend', 'neutral')
    return last != new_trend


def _count_bearish_days():
    """
    Cuenta cuántos días consecutivos lleva BTC en tendencia bajista
    (precio < EMA20_4h Y precio < EMA50_4h en velas 4h consecutivas desde ahora).
    """
    try:
        k4h = utils.get_klines('BTCUSDT', interval='4h', limit=60)
        closes = [float(k[4]) for k in k4h]
        ema20  = utils.ema(closes, 20)
        ema50  = utils.ema(closes, 50)
        bearish_candles = 0
        for i in range(len(closes) - 1, -1, -1):
            if closes[i] < ema20[i] and closes[i] < ema50[i]:
                bearish_candles += 1
            else:
                break
        return bearish_candles * 4 / 24
    except Exception:
        return 0.0


def _count_bullish_days():
    """
    Cuenta cuántos días consecutivos lleva BTC en tendencia alcista
    (precio > EMA20_4h Y precio > EMA50_4h en velas 4h consecutivas desde ahora).
    """
    try:
        k4h = utils.get_klines('BTCUSDT', interval='4h', limit=60)
        closes = [float(k[4]) for k in k4h]
        ema20  = utils.ema(closes, 20)
        ema50  = utils.ema(closes, 50)
        bullish_candles = 0
        for i in range(len(closes) - 1, -1, -1):
            if closes[i] > ema20[i] and closes[i] > ema50[i]:
                bullish_candles += 1
            else:
                break
        return bullish_candles * 4 / 24
    except Exception:
        return 0.0


def rebalance(state, btc_ctx=None):
    """
    Evalúa si corresponde rebalancear y ejecuta la transferencia si es necesario.
    Retorna (transferido: bool, mensaje: str)
    """
    if btc_ctx is None:
        btc_ctx = market.get_btc_context()

    trend = btc_ctx.get('trend', 'neutral')

    spot_free, fut_free, spot_in_pos, fut_actual = _capital_total(state)

    # Capital total real (libre + atrapado en posiciones)
    spot_actual   = spot_free + spot_in_pos
    total_capital = spot_actual + fut_actual

    # Capital libre para operar
    total_free = spot_free + fut_free

    if total_free < REBALANCE_MIN_WALLET * 2:
        return False, f'Capital libre insuficiente para rebalancear (${total_free:.2f})'

    # ── Targets calculados sobre capital TOTAL ────────────────────────────────
    # Modo direccional: concentrar 100% en la wallet de la tendencia
    if config.DIRECTIONAL_MODE and trend == 'bearish':
        ratio_fut = 1.0  # 100% futures
        label = f'direccional bearish → {ratio_fut*100:.0f}% futures'
        target_fut  = total_capital * ratio_fut
        target_spot = total_capital * (1 - ratio_fut)
    elif config.DIRECTIONAL_MODE and trend == 'bullish':
        ratio_spot = 1.0  # 100% spot
        label = f'direccional bullish → {ratio_spot*100:.0f}% spot'
        target_spot = total_capital * ratio_spot
        target_fut  = total_capital * (1 - ratio_spot)
    elif trend == 'bearish':
        # Detectar cuántos días consecutivos lleva bajista
        bearish_days = _count_bearish_days()
        if bearish_days >= VERY_BEARISH_DAYS:
            ratio_fut = RATIO_VERY_BEARISH_FUTURES
            label = f'muy bajista ({bearish_days:.1f} días) → {ratio_fut*100:.0f}% futures'
        else:
            ratio_fut = RATIO_BEARISH_FUTURES
            label = f'bearish ({bearish_days:.1f} días) → {ratio_fut*100:.0f}% futures'
        target_fut  = total_capital * ratio_fut
        target_spot = total_capital * (1 - ratio_fut)
    elif trend == 'bullish':
        if config.DIRECTIONAL_MODE:
            ratio_spot = 1.0  # 100% spot
            label = f'direccional bullish → {ratio_spot*100:.0f}% spot'
            target_spot = total_capital * ratio_spot
            target_fut  = total_capital * (1 - ratio_spot)
        else:
            bullish_days = _count_bullish_days()
            if bullish_days >= VERY_BULLISH_DAYS:
                ratio_spot = RATIO_VERY_BULLISH_SPOT
                label = f'muy alcista ({bullish_days:.1f} días) → {ratio_spot*100:.0f}% spot'
            else:
                ratio_spot = RATIO_BULLISH_SPOT
                label = f'bullish ({bullish_days:.1f} días) → {ratio_spot*100:.0f}% spot'
            target_spot = total_capital * ratio_spot
            target_fut  = total_capital * (1 - ratio_spot)
    else:
        target_spot = total_capital * 0.5
        target_fut  = total_capital * 0.5
        label = 'neutral → 50/50'

    # Capital actual real (libre + en posiciones) por wallet
    # spot_actual y fut_actual ya calculados arriba desde _capital_total
    diff_fut = target_fut - fut_actual   # positivo = futures tiene menos de lo que debería

    if abs(diff_fut) < REBALANCE_MIN_USDT:
        return False, f'Balances ya alineados ({trend}): spot=${spot_actual:.2f} fut=${fut_actual:.2f}'

    # ── Cambio de tendencia: rebalanceo PACIENTE ──────────────────────────────
    # Si hay posiciones de la dirección "vieja" abiertas, no forzar nada.
    # Solo transferir el capital que ya está libre, progresivamente.
    trend_flipped = _trend_changed(state, trend)
    shorts_open = [p for p in state.get('positions', []) if p['direction'] == 'short']
    longs_open  = [p for p in state.get('positions', []) if p['direction'] == 'long']

    if diff_fut > 0:
        # Necesitamos más capital en futures (bearish) — mover Spot → Futures
        amount = round(min(diff_fut, spot_free - REBALANCE_MIN_WALLET), 2)
        if amount < REBALANCE_MIN_USDT:
            if trend_flipped and longs_open:
                # Cambio a bearish pero hay longs viejos: esperar que cierren
                return False, (
                    f'⏳ Tendencia viró a BEARISH — esperando cierre de {len(longs_open)} long(s) ' 
                    f'para liberar capital spot. Rebalanceo progresivo en curso.'
                )
            return False, f'No hay suficiente USDT libre en spot para transferir (${spot_free:.2f})'

        if longs_open and spot_free - amount < REBALANCE_MIN_WALLET:
            return False, f'No se puede reducir spot: hay {len(longs_open)} long(s) activo(s)'

        try:
            utils.spot_signed('POST', '/sapi/v1/asset/transfer', {
                'type': 'MAIN_UMFUTURE', 'asset': 'USDT', 'amount': str(amount),
            })
            state['last_rebalance_trend'] = trend
            if trend_flipped:
                msg = (f'🔄 Rebalanceo parcial (viraje a BEARISH): ${amount:.2f} Spot → Futures ' 
                       f'| Quedan {len(longs_open)} longs — capital liberará al cerrar')
            else:
                msg = f'🔄 Rebalanceo ({label}): ${amount:.2f} USDT Spot → Futures'
            return True, msg
        except Exception as e:
            return False, f'Error al transferir Spot→Futures: {e}'

    else:
        # Necesitamos más capital en spot (bullish) — mover Futures → Spot
        amount = round(min(-diff_fut, fut_free - REBALANCE_MIN_WALLET), 2)
        if amount < REBALANCE_MIN_USDT:
            if trend_flipped and shorts_open:
                # Cambio a bullish pero hay shorts viejos: esperar que cierren
                return False, (
                    f'⏳ Tendencia viró a BULLISH — esperando cierre de {len(shorts_open)} short(s) '
                    f'para liberar capital futures. Rebalanceo progresivo en curso.'
                )
            return False, f'No hay suficiente USDT libre en futures para transferir (${fut_free:.2f})'

        if shorts_open and fut_free - amount < REBALANCE_MIN_WALLET:
            return False, (
                f'⏳ Futures ocupado ({len(shorts_open)} short(s) con margen). '
                f'Transferiré ${amount:.2f} cuando cierren posiciones.'
            )

        try:
            utils.spot_signed('POST', '/sapi/v1/asset/transfer', {
                'type': 'UMFUTURE_MAIN', 'asset': 'USDT', 'amount': str(amount),
            })
            state['last_rebalance_trend'] = trend
            if trend_flipped:
                msg = (f'🔄 Rebalanceo parcial (viraje a BULLISH): ${amount:.2f} Futures → Spot '
                       f'| Quedan {len(shorts_open)} shorts — capital liberará al cerrar')
            else:
                msg = f'🔄 Rebalanceo ({label}): ${amount:.2f} USDT Futures → Spot'
            return True, msg
        except Exception as e:
            return False, f'Error al transferir Futures→Spot: {e}'


if __name__ == '__main__':
    import json
    with open(config.STATE_FILE) as f:
        state = json.load(f)
    ok, msg = rebalance(state)
    print(msg)
