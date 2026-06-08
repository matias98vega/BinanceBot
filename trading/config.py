#!/usr/bin/env python3
"""
Configuración centralizada del bot de trading.
Todos los parámetros ajustables están acá.
"""

# ── API ──────────────────────────────────────────────────────────────────────
import os, json

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
API_KEY    = _env.get('BINANCE_API_KEY',    os.environ.get('BINANCE_API_KEY',    '0DwLCZ1RnGhfnWygp3PUxPrLGLjLByukBFvjEo06p5fVQpsICjdcKBLBRwXzOnVr'))
API_SECRET = _env.get('BINANCE_API_SECRET', os.environ.get('BINANCE_API_SECRET', 'VCMhz7vCQZGgwAIV4PDY74bpRGOxDY0gT4rh6a5cLJmh2mCfcJF1uQu3qhzcQWmM'))

SPOT_BASE    = 'https://api.binance.com'
FUTURES_BASE = 'https://fapi.binance.com'

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
STATE_FILE   = os.path.join(BASE_DIR, 'state.json')
TRADES_LOG   = os.path.join(BASE_DIR, 'trades_log.txt')
ANALYSIS_LOG = os.path.join(BASE_DIR, 'analysis_log.txt')
LOCK_FILE    = '/tmp/trading_bot.lock'

# ── Alertas ──────────────────────────────────────────────────────────────────
ALERT_TARGET = '20313075:thread:019e6042-4a09-7808-a0e4-dea13cade83b'

# ── Modo dry-run (sin órdenes reales) ────────────────────────────────────────
DRY_RUN              = False  # True = simular sin ejecutar órdenes

# ── Capital ──────────────────────────────────────────────────────────────────
SPOT_RISK_PCT        = 0.93   # % del capital spot a usar por trade long
FUTURES_RISK_PCT     = 0.50   # % del capital futures a usar por trade short (por posición)
FUTURES_LEVERAGE     = 2      # apalancamiento (empieza en 2x)
SPOT_RISK_REDUCED    = 0.50   # capital tras N SL consecutivos
MAX_CONSEC_SL        = 2      # SL consecutivos antes de reducir riesgo

# ── Diversificación dinámica por capital ─────────────────────────────────────────
DIVERSIFY_THRESHOLD_1 = 30.0  # por encima de este capital libre: hasta 2 posiciones
DIVERSIFY_THRESHOLD_2 = 50.0  # por encima de este capital total: hasta 3 posiciones
DIVERSIFY_RISK_2      = 0.45  # % por posición cuando hay 2 simultáneas
DIVERSIFY_RISK_3      = 0.30  # % por posición cuando hay 3 simultáneas

# ── Limpieza de polvo (dust) ────────────────────────────────────────────────────
DUST_CLEAN_DAY        = 0     # día de la semana para limpiar polvo (0=lunes)
DUST_MIN_VALUE_USD    = 0.10  # no convertir si el total de polvo es menor a este valor
DUST_PROTECTED        = {'USDT', 'USDC', 'BNB'}  # nunca convertir estos activos

# ── Posiciones simultáneas ───────────────────────────────────────────────────
MAX_LONG_POSITIONS   = 2      # máx posiciones long abiertas al mismo tiempo
MAX_SHORT_POSITIONS  = 2      # máx posiciones short abiertas al mismo tiempo
# Nota: no se abre long Y short en el mismo símbolo

# ── SL / TP ──────────────────────────────────────────────────────────────────
SL_ATR_MULT          = 1.0    # multiplier ATR para SL de longs
SL_ATR_MULT_SHORT    = 1.5    # multiplier ATR para SL de shorts (más margen contra spikes)
TP_ATR_MULT          = 2.0
SL_MIN_DIST_PCT      = 1.0    # distancia mínima SL desde entrada (%)
PARTIAL_TAKE_PCT     = 0.5    # cerrar esta fracción en TP1 (50%)

# ── Filtros de entrada ───────────────────────────────────────────────────────
BTC_MOMENTUM_PAUSE_PCT    = 2.0  # pausar entradas si BTC se mueve >2% en la ventana
BTC_MOMENTUM_CLOSE_PCT    = 4.0  # cerrar SHORTS existentes si BTC sube >4% (pump extremo)
BTC_MOMENTUM_CLOSE_LONGS  = -4.0 # cerrar LONGS existentes si BTC baja >4% (dump extremo)
BTC_MOMENTUM_WINDOW_H     = 4    # ventana de tiempo para medir momentum (horas)

# ── Modo direccional (trend-following) ─────────────────────────────────────
# Si True: solo opera en dirección de la tendencia (shorts en bearish, longs en bullish)
# Si False: opera ambos lados simultáneamente (sistema actual)
DIRECTIONAL_MODE       = True   # ⚙️ SWITCH: cambiar a True para activar modo direccional
DIRECTIONAL_NEUTRAL_BOTH = True  # en neutral, permitir ambos lados si DIRECTIONAL_MODE=True

# ── Filtros de entrada ───────────────────────────────────────────────────────
RSI_MAX_LONG         = 65     # no entrar long si RSI > 65
RSI_MIN_SHORT        = 42     # no entrar short si RSI < 42 (evitar entrar en sobrevendido)
ATR_MIN_PCT          = 0.5    # mercado mínimamente volátil
ATR_MAX_PCT          = 3.5    # no entrar si ATR > 3.5% (evita entries en volatilidad extrema)
SCORE_MIN            = 5      # score mínimo para entrar (largo o corto)
SCORE_MIN_VOLATILE   = 6      # score mínimo cuando ATR_4h > 3%
SCORE_MIN_COUNTER    = 11     # score mínimo para entrar contra el contexto macro (bearish=long / bullish=short)
ATR_VOLATILE_THRESH  = 3.0    # % ATR 4h para considerar mercado volátil

# ── Trailing stop ────────────────────────────────────────────────────────────
TRAIL_STEP_PCT       = 1.0    # actualizar SL cada 1% de movimiento favorable

# ── SL nativo en futures (STOP_MARKET en el exchange) ────────────────────────
NATIVE_SL_ENABLED    = True   # True = STOP_MARKET nativo; False = solo guardian software

# ── Filtro de recuperación desde mínimo (anti-short en rebote) ───────────────
RECOVERY_FROM_LOW_PCT     = 3.0  # si precio rebotó >3% desde mínimo 24h → penalizar short
RECOVERY_CONSEC_CANDLES   = 3    # velas 1h consecutivamente alcistas desde el mínimo → subir min_score

# ── Protecciones ─────────────────────────────────────────────────────────────
DAILY_LOSS_LIMIT_PCT = 5.0    # pausar si PnL del día cae >5% del capital inicial
STALE_HOURS          = 5      # salir si trade lleva +5h sin moverse
STALE_RANGE_PCT      = 0.5    # rango de "estancado" en %
STALE_MAX_HOURS      = 12     # salir SIEMPRE después de 12h (aunque esté en profit)
COOLDOWN_AFTER_SL    = True   # no reentrar en el mismo par tras un SL
COOLDOWN_HOURS       = 8      # horas que dura el cooldown tras un SL
BNB_FEE_RATE         = 0.00075  # fee spot con BNB
FUTURES_FEE_RATE     = 0.0004   # fee futures taker
OCO_MAX_RETRIES      = 3

# ── Correlación BTC ──────────────────────────────────────────────────────────
BTC_CORR_MAX         = 0.85   # descartar si correlación con BTC > 0.85 y BTC débil
BTC_WEAK_PCT         = -0.5   # BTC débil si cambio 4h < -0.5%
BTC_STRONG_PCT       = 0.5    # BTC fuerte si cambio 4h > 0.5%
BTC_REBOUND_1H_PCT   = 0.3    # BTC en rebote si cambio 1h > 0.3% → penalizar nuevos shorts

# ── Contexto macro ───────────────────────────────────────────────────────────
# Si BTC cae/sube más de este % en 4h, modo forzado (solo shorts / solo longs)
BTC_CRASH_PCT        = -5.0
BTC_PUMP_PCT         = 5.0

# ── Blacklist de tokens (excluidos permanentemente) ──────────────────────────
BLACKLIST_SYMBOLS    = {
    'VICUSDT',    # microcap extrema, SL -$1.12
    'HOMEUSDT',   # microcap, SL -$0.88
    'GUNUSDT',    # microcap, SL -$0.27
    'TONUSDT',    # 2 SLs consecutivos -$0.84 total
    'SKYAIUSDT',  # volatilidad extrema 6.89%/h, rango 118% en 48h, cerrado manual -$1.20
}

# ── Tokens de acciones US tokenizadas (sujetos a horario de mercado) ────────
# Apertura US: 14:30 UTC | Cierre: 21:00 UTC
# Ventana riesgosa: primeros 30 min de apertura (14:30–15:00 UTC)
US_STOCK_TOKENS      = {
    'INTCUSDT', 'NVDAUSDT', 'SOXLUSDT', 'AAPLUSDT', 'TSLALUSDT',
    'MSTRUSDT', 'COINUSDT', 'MSFLUSDT', 'AMZNUSDT', 'GOOGLLUSDT',
}
US_MARKET_OPEN_UTC   = (14, 30)   # hora, minuto
US_MARKET_AVOID_MIN  = 45         # evitar entrar en los primeros N minutos de apertura

# Criterio para entrar acá: σ horaria >3% O rango 48h >50% O meme/AI hypeado
# O múltiples SLs recurrentes (≥3 en 5 días)
RISKY_SYMBOLS        = {
    # Agregar aquí tokens que pasen los filtros pero necesiten más cuidado
    # 'PEPEUSDT', 'WIFUSDT', etc.
    'FETUSDT',   # 4 SLs en 3 días — volatilidad estructural alta
}

# ── Filtros extra para RISKY_SYMBOLS ─────────────────────────────────────────
RISKY_SCORE_BONUS    = 2      # puntos adicionales requeridos (encima del mínimo normal)
RISKY_RISK_FACTOR    = 0.50   # reducir capital a 50% para tokens riesgosos
RISKY_VOL_HOURLY_MAX = 4.0    # volatilidad horaria máxima permitida (%) — auto-blacklist si supera
RISKY_RANGE_48H_MAX  = 60.0   # rango 48h máximo permitido (%) — auto-blacklist si supera

# ── Riesgo reducido en contexto bajista para longs ────────────────────────────
SPOT_RISK_BEARISH    = 0.50   # % capital por long cuando contexto=bearish (vs 0.93 normal)

# ── Red ──────────────────────────────────────────────────────────────────────
NET_RETRIES          = 3
NET_RETRY_DELAY      = 2.0
