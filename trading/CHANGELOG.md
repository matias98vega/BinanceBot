# CHANGELOG — Trading Bot

## v2.0.0 — 2026-05-31 (refactor completo)

### Arquitectura
- Reemplaza `auto_loop.py` + watchers individuales por un bot modular unificado
- Módulos: `config.py`, `utils.py`, `market.py`, `longs.py`, `shorts.py`, `bot.py`, `sl_guardian.py`
- Estado unificado en `state.json` con lista de posiciones activas (antes: una sola posición)
- Crons: `trading-bot` cada 10 min + `sl-guardian` cada 2 min

### Features nuevos
- **Longs y shorts simultáneos** — spot + futures independientes, sin pisarse
- **SL Guardian** — vigía de SL cada 2 min, independiente del bot principal
- **Take profit parcial** — cierra 50% al llegar a la mitad del recorrido hacia el TP, mueve SL a breakeven
- **Trailing stop** — actualiza SL cada 1% de movimiento favorable (long y short)
- **Cooldown dinámico** — expira automáticamente en 4h tras un SL (antes era permanente)
- **Lista dinámica de candidatos** — top 40 pares por volumen 24h (antes lista hardcodeada de 20)
- **Filtro multi-timeframe 15m** — confirma momentum antes de entrar; si falla, sube el score mínimo requerido
- **Scoring de shorts mejorado** — death cross, divergencia bajista RSI, volumen en distribución, EMA en 4 niveles (EMA20/50 en 1h y 4h)
- **Modo dry-run** — `config.DRY_RUN = True` simula órdenes sin ejecutar
- **Modo forzado por contexto** — si BTC cae >5% en 4h: solo shorts; si sube >5%: solo longs
- **Límite de pérdida diaria** — pausa automática si PnL del día cae >5%
- **Reducción de riesgo** — usa 50% del capital tras 2 SL consecutivos

### Fixes
- OCO spot: cambiado de `/api/v3/orderList/oco` (nuevo, rechazado) a `/api/v3/order/oco` (clásico)
- SL futures: `STOP_MARKET` no disponible en esta cuenta (error -4120); reemplazado por monitoreo de precio + cierre MARKET
- TP futures: orden `LIMIT + reduceOnly=true` (funciona correctamente)
- Fill price asíncrono: agregado polling post-fill para obtener `avgPrice` real
- Cooldown migrado de lista a dict `{symbol: expiry_timestamp}`

### Parámetros clave (config.py)
| Parámetro | Valor | Descripción |
|---|---|---|
| FUTURES_LEVERAGE | 2 | Apalancamiento inicial |
| FUTURES_RISK_PCT | 0.50 | % capital futures por trade |
| SPOT_RISK_PCT | 0.93 | % capital spot por trade |
| MAX_LONG_POSITIONS | 2 | Máx longs simultáneos |
| MAX_SHORT_POSITIONS | 2 | Máx shorts simultáneos |
| SCORE_MIN | 5 | Score mínimo para entrar |
| COOLDOWN_HOURS | 4 | Duración cooldown post-SL |
| PARTIAL_TAKE_PCT | 0.5 | Fracción a cerrar en TP1 |
| TRAIL_STEP_PCT | 1.0 | % movimiento para subir SL |
| STALE_HOURS | 8 | Salir si trade estancado N horas |
| DAILY_LOSS_LIMIT_PCT | 5.0 | % pérdida diaria para pausar |
| DRY_RUN | False | Simular sin ejecutar órdenes |

---

## v1.x — historial previo

### Watchers individuales (deprecados)
- `snx-sl-watcher.py` — monitoreo SL para SNXUSDT short
- `btcdom-sl-watcher.py` — monitoreo SL para BTCDOMUSDT short
- `sol-sl-watcher.py` — monitoreo SL para SOLUSDT short
- `auto_loop.py` — bot de longs en spot (una posición a la vez)
