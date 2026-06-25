# BinanceBot Architecture

Este documento describe la arquitectura actual del bot. Es una referencia de mantenimiento: no define cambios de estrategia ni recomendaciones operativas.

## Flujo completo de una iteracion

1. `bot.py:main()` toma un lock con `utils.acquire_lock()` para evitar ejecuciones concurrentes.
2. `bot.py:_run()` carga `state.json` con `utils.load_state()`.
3. Migra `cooldown_symbols` si el formato anterior era lista.
4. Ejecuta reset diario si cambio `pnl_date`: reinicia PnL diario, reactiva el bot si estaba pausado y calcula `daily_start_capital`.
5. Ejecuta `_audit_orphans(state)` para detectar activos spot no registrados y protegerlos con OCO.
6. Revisa la blacklist dinamica con `market.review_dynamic_blacklist()` cada 6 horas.
7. Si `state.status == paused`, guarda estado y finaliza.
8. Aplica circuit breaker si `consec_sl >= 4`: pausa 24h, alerta, registra evento de analytics y termina.
9. Si habia pausa por circuit breaker, verifica expiracion y reactiva si corresponde.
10. Obtiene contexto BTC con `market.get_btc_context()`.
11. Ejecuta rebalanceo de capital con `rebalance.rebalance(state, btc_ctx)`.
12. Revisa cierre preventivo por momentum extremo de BTC con `market.check_btc_momentum_close(btc_ctx)`.
13. Gestiona cada posicion activa:
    - Long: `_check_partial_long(pos, state)` y `longs.manage_long(pos, state)`.
    - Short: `_check_partial_short(pos, state)` y `shorts.manage_short(pos, state)`.
14. Si una posicion retorna `closed_tp`, `closed_sl` o `closed_manual`, `_handle_close(...)` actualiza PnL, logs, cooldowns y analytics.
15. Si una posicion retorna `updated`, se conserva con SL/trailing actualizado.
16. Si sigue en `hold`, se conserva y se imprime estado.
17. Recalcula capital, cooldowns y limites de posiciones.
18. Si no hay pausa post-SL, escanea LONG con `market.scan_longs(...)`.
19. Si hay candidato LONG, abre con `longs.open_long(...)`, agrega a `state.positions`, registra analytics y alerta.
20. Si no hay pausa post-SL, escanea SHORT con `market.scan_shorts(...)`.
21. Si hay candidato SHORT, abre con `shorts.open_short(...)`, agrega a `state.positions`, registra analytics y alerta.
22. Imprime resumen, ejecuta limpieza de polvo si corresponde, guarda `state.json` y libera lock.

`sl_guardian.py` corre por separado y con mayor frecuencia. Solo revisa SLs de posiciones existentes; si cierra una posicion, actualiza `state.json`, cooldowns y analytics.

## Archivos y responsabilidades

| Archivo | Responsabilidad |
|---|---|
| `bot.py` | Orquestador principal. Controla ciclo, locks, reset diario, gestion de posiciones, entradas, cierres, alerts, state y analytics. |
| `market.py` | Contexto BTC, seleccion de candidatos, indicadores, scoring, filtros de mercado, blacklist dinamica y riesgo de volatilidad. |
| `longs.py` | Apertura y gestion de posiciones long spot. Compra MARKET, OCO TP/SL, trailing, stale exit y recolocacion de OCO. |
| `shorts.py` | Apertura y gestion de posiciones short futures. Orden MARKET SELL, TP LIMIT reduceOnly, SL nativo o guardian, trailing y stale exit. |
| `sl_guardian.py` | Proceso liviano de proteccion. Cierra posiciones que tocaron SL si el flujo principal no las cerro antes. |
| `rebalance.py` | Rebalancea USDT entre spot y futures segun contexto BTC y posiciones abiertas. No abre ni cierra trades. |
| `utils.py` | API Binance, firmas, precios, balances, filtros, indicadores base, lock, estado, cooldowns, logs y alertas. |
| `config.py` | Constantes configurables, rutas, credenciales/env, umbrales, parametros de riesgo, filtros y listas. |
| `config_loader.py` | Loader centralizado de variables de entorno y `.env`. Valida credenciales, endpoints y rutas sin hardcodear secretos. |
| `analytics.py` | Telemetria estructurada append-only, decision snapshots, export CSV y helpers de reconstruccion. |
| `analyze_trades.py` | Reporte estadistico sobre `trade_analytics.jsonl`. |
| `analyze_decisions.py` | Reporte estadistico sobre `decision_snapshots.jsonl`. |
| `validate_observability.py` | Valida integridad local de archivos JSONL, campos requeridos, duplicados y tamanos. |
| `healthcheck.py` | Healthcheck local de estado, lock, edades de logs y alineacion state vs analytics. |
| `reconcile_existing_positions.py` | Reconciliacion local de posiciones abiertas que existian antes de analytics. Agrega registros `OPEN` recuperados sin tocar estrategia ni `state.json`. |
| `preflight_check.py` | Orquestador local previo a una iteracion real. Ejecuta healthcheck, validacion y analizadores sin conectarse a Binance. |
| `post_cycle_check.py` | Verificacion local posterior a una iteracion real. Compara contadores actuales contra `.cycle_baseline.json`. |
| `setup_check.py` | Verificacion de despliegue: Python, dependencias, `.env`, archivos, permisos, ping Binance y autenticacion de API sin operar. |
| `daily_summary.py` | Resumen diario basado en logs/estado. |

## Comunicacion entre modulos

- `bot.py` importa `config`, `utils`, `market`, `longs`, `shorts`, `rebalance` y `AnalyticsLogger`.
- `config.py` carga credenciales, endpoints y rutas desde `config_loader.py`; no contiene API keys ni secrets hardcodeados.
- `market.py`, `longs.py`, `shorts.py`, `rebalance.py` y `sl_guardian.py` usan `utils.py` para hablar con Binance.
- `config.py` es leido por todos los modulos principales.
- `state.json` es la fuente local de posiciones abiertas y acumuladores.
- `trades_log.txt` es el log humano historico.
- `trade_analytics.jsonl` es el log estructurado append-only para analitica.
- `decision_snapshots.jsonl` es el log estructurado append-only de decisiones aceptadas, rechazadas y skipped.

## Flujo de informacion

1. Binance publica precios/klines/balances.
2. `utils.py` obtiene datos publicos y privados.
3. `market.py` transforma klines en contexto, indicadores, score y candidatos.
4. `bot.py` decide si evalua entradas o solo gestiona posiciones existentes.
5. `longs.py` y `shorts.py` ejecutan ordenes y retornan `pos` o acciones de cierre.
6. `bot.py` actualiza `state.json` y escribe logs.
7. `analytics.py` agrega eventos JSONL sin modificar el estado operativo.
8. `analyze_trades.py` y `analytics.py --export` leen JSONL y producen reportes.
9. `analyze_decisions.py` lee snapshots de decision y resume rechazos, scores y campos nulos.
10. `validate_observability.py` y `healthcheck.py` auditan archivos locales antes de operar, sin conectarse a Binance.
11. `preflight_check.py` consolida esas auditorias antes de correr una iteracion real.
12. `post_cycle_check.py` guarda una baseline local y compara despues del ciclo si snapshots, analytics y estado siguen alineados.

## Almacenamiento

### `state.json`

Archivo JSON mutable. Se carga con `utils.load_state()` y se guarda con `utils.save_state()`. Contiene:

- `positions`: posiciones abiertas.
- `trade_count`: contador historico usado por `trades_log.txt`.
- `total_pnl_usdt`, `daily_pnl_usdt`, `daily_start_capital`, `pnl_date`.
- `consec_sl`, `last_sl_time`, `skip_next_cycles`.
- `cooldown_symbols`.
- `status`, `pause_until`.
- Campos auxiliares: blacklist review, limpieza de polvo, historial SL por simbolo.

No se debe usar como historico completo de trades cerrados; solo representa estado operativo actual.

### `trades_log.txt`

Log humano append-only. Se escribe con `utils.log_trade(...)` y algunas rutas manuales en `bot.py`. Registra resultado, PnL, capital posterior y fecha. Es util para lectura rapida, pero no es ideal para analitica estructurada.

### `trade_analytics.jsonl`

Log estructurado append-only. Cada linea es un JSON independiente.

- Aperturas: `status = OPEN`.
- Cierres: `status = CLOSED`.
- Eventos generales: `event_type`, por ejemplo `CIRCUIT_BREAKER`.
- Campos de entrada enriquecidos cuando existen: `ema20`, `ema50`, `volume_ratio`, `macd_hist`, `atr_pct`, `btc_correlation`, `reject_reason`, `reject_reasons`.

No reescribe el archivo completo. `AnalyticsLogger._merged_trades()` reconstruye el estado final por `trade_id` al exportar o analizar.

### `decision_snapshots.jsonl`

Log estructurado append-only de observabilidad por iteracion activa. Lo escribe `DecisionSnapshotLogger.log_snapshot(...)` desde `bot._safe_log_decision_snapshot(...)`.

Cada snapshot contiene:

- Contexto: `timestamp`, `market_regime`, `btc_change_1h`, `btc_change_4h`, `mode`.
- Capital: `capital_total`, `spot_balance`, `futures_balance`.
- Candidatos: lista limitada de accepted/rejected/skipped con score, indicadores y reason.

Para limitar tamano, `market._limit_decisions(...)` conserva:

- candidatos aceptados,
- rechazos por poco margen,
- top 20 por score,
- descartes por filtros importantes,
- maximo aproximado de 40 registros por lado antes de escribir el snapshot.

`analyze_decisions.py` resume cantidad de snapshots, aceptados, rechazados, razones frecuentes, score promedio por simbolo, distribucion LONG/SHORT y nulls por campo importante.

### `.cycle_baseline.json`

Archivo JSON local generado por `post_cycle_check.py --save-baseline` antes de ejecutar una iteracion real del bot. Contiene:

- `timestamp`,
- cantidad de lineas de `trade_analytics.jsonl`,
- cantidad de lineas de `decision_snapshots.jsonl`,
- tamano de ambos archivos,
- posiciones abiertas en `state.json`,
- trades `OPEN` en analytics,
- lineas corruptas JSONL conocidas al momento de la baseline.

No participa en la estrategia ni en el estado operativo. Solo permite comparar el estado local antes/despues de un ciclo real.

### `.env`

Archivo local no versionado. Es cargado por `config_loader.py` desde la raiz del proyecto o desde `trading/.env`. Contiene credenciales, endpoints y rutas configurables.

Variables principales:

- `BINANCE_API_KEY`,
- `BINANCE_API_SECRET`,
- `BINANCE_SPOT_BASE`,
- `BINANCE_FUTURES_BASE`,
- `STATE_FILE`,
- `TRADES_LOG`,
- `ANALYSIS_LOG`,
- `ANALYTICS_FILE`,
- `DECISION_SNAPSHOTS_FILE`,
- `REPORTS_DIR`,
- `CSV_FILE`,
- `CYCLE_BASELINE_FILE`,
- `LOCK_FILE`,
- `ALERT_TARGET`.

`.env.example` documenta las variables sin incluir credenciales reales.

### `validate_observability.py`

Script local de integridad. No se conecta a Binance. Valida:

- existencia de `trade_analytics.jsonl` y `decision_snapshots.jsonl`,
- JSONL corrupto,
- cierres sin apertura,
- opens/closes duplicados por `trade_id`,
- eventos `OPEN`, eventos `CLOSED` y trades actualmente abiertos reconstruidos por ultimo estado,
- campos requeridos en trades abiertos y cerrados,
- nulls en campos de indicadores enriquecidos,
- snapshots sin candidatos,
- tamanos de archivos principales.

El resultado final puede ser `OK`, `WARNING` o `ERROR`.

### `healthcheck.py`

Script local de salud operativa. No se conecta a Binance. Revisa:

- existencia y validez JSON de `state.json`,
- presencia de lock local,
- edad de `trade_analytics.jsonl`, `decision_snapshots.jsonl` y `trades_log.txt`,
- posiciones abiertas en `state.json`,
- trades `OPEN` en analytics,
- desalineacion evidente entre posiciones abiertas del state y trades abiertos en analytics.
- cantidad de trades `OPEN` marcados como recuperados por reconciliacion inicial.

### `preflight_check.py`

Script local previo a una iteracion real. Ejecuta en orden:

1. `healthcheck.py`.
2. `validate_observability.py`.
3. `analyze_trades.py`.
4. `analyze_decisions.py`.

Resume los estados como `OK`, `WARNING` o `ERROR`. Un `ERROR` en healthcheck u observabilidad bloquea el estado final. Los `WARNING` por campos `null` en posiciones recuperadas no se consideran error porque esos datos no existian en memoria y no deben inventarse.

### `post_cycle_check.py`

Script local posterior a una iteracion real. No se conecta a Binance. Tiene dos modos:

- `post_cycle_check.py --save-baseline`: guarda `.cycle_baseline.json` con contadores locales antes del ciclo.
- `post_cycle_check.py`: compara el estado actual contra la baseline y reporta diferencias.

Reporta:

- snapshots antes/despues y delta,
- cantidad actual de snapshots,
- si el ultimo snapshot tiene candidatos,
- accepted/rejected/skipped del ultimo snapshot,
- trades `OPEN` en analytics,
- posiciones abiertas en `state.json`,
- alineacion state vs analytics por `trade_id`,
- nuevas lineas corruptas JSONL,
- tamanos actuales de `decision_snapshots.jsonl` y `trade_analytics.jsonl`.

### `setup_check.py`

Script de despliegue. Ejecuta verificaciones pasivas:

- version de Python,
- dependencias de biblioteca estandar usadas por el proyecto,
- presencia de `.env` y variables obligatorias,
- existencia de archivos locales principales,
- permisos de escritura en directorios de datos,
- `GET /api/v3/ping` contra Binance,
- autenticacion de API con `GET /api/v3/account`.

No abre operaciones, no modifica balances y no modifica ordenes.

### `reconcile_existing_positions.py`

Script local de reconciliacion inicial. No se conecta a Binance y no modifica `state.json`.

Flujo:

1. Lee `state.json`.
2. Lee `trade_analytics.jsonl`.
3. Detecta posiciones abiertas en `state.json` cuyo `id` no exista como `trade_id` con `status = OPEN` en analytics.
4. Agrega una linea `OPEN` append-only por cada posicion faltante.
5. Marca cada registro con:
   - `recovered_existing_position = true`,
   - `recovery_reason = position_existed_before_analytics`,
   - `analytics_recovered_at`.
6. Usa `entry_time`, `entry_price`, `atr`, `quantity`, `sl` y `tp` solo si ya existen en `state.json`.
7. Mantiene `null` para indicadores que no estaban disponibles en memoria, como `score`, `rsi`, `ema20`, `ema50`, `volume_ratio`, `macd_hist`, `atr_pct` y `btc_correlation`.
8. Es idempotente porque no agrega otro registro si ya existe un `OPEN` con el mismo `trade_id`.

El objetivo es alinear observabilidad historica con posiciones abiertas previas a la implementacion de analytics, sin cambiar condiciones de entrada, salida, filtros, gestion de riesgo ni comportamiento operativo.

## Flujo de decisiones

### Escaneo mercado

- LONG: `bot.py:_run()` llama `market.scan_longs(btc_ctx, excluded_symbols=excluded)`.
- SHORT: `bot.py:_run()` llama `market.scan_shorts(btc_ctx, excluded_symbols=excl_short)`.
- Candidatos dinamicos: `market.get_dynamic_candidates(top_n=40)`.
- Fallback: `_STATIC_CANDIDATES` en `market.py`.

### Clasificacion del mercado

- `market.get_btc_context()` calcula:
  - `trend`: `bullish`, `bearish`, `neutral`.
  - `btc_price`.
  - `ema20_4h`, `ema50_4h`.
  - `atr_4h`.
  - `change_4h`, `change_1h`.
  - `force_mode`: `long_only`, `short_only` o `None`.

### Seleccion LONG / SHORT

- `market.scan_longs(...)` aplica pausas, modo direccional, blacklist, volatilidad, scoring y filtros.
- `market.scan_shorts(...)` aplica el flujo equivalente para shorts.
- Seleccion final:
  - LONG: `max(results, key=lambda x: (x['score'], x['atr_pct']))`.
  - SHORT: `max(results, key=lambda x: (x['score'], x['atr_pct']))`.

### Score

- LONG: `market.score_long(symbol, btc_ctx)`.
- SHORT: `market.score_short(symbol, btc_ctx)`.
- Confirmacion corta: `market.confirm_15m(symbol, direction, futures=...)`.

### Filtros

- Globales/mercado: `check_btc_momentum`, modo direccional, `force_mode`.
- Riesgo de simbolo: blacklist estatica, blacklist dinamica, `check_volatility_risk`.
- Indicadores: ATR, RSI, correlacion BTC, volumen relativo, score minimo.
- Contexto: contra tendencia usa `SCORE_MIN_COUNTER`.

### Entrada

- LONG spot: `longs.open_long(candidate, state)`.
  - Calcula capital.
  - Ejecuta BUY MARKET.
  - Coloca OCO SELL TP/SL.
  - Retorna `pos`.
- SHORT futures: `shorts.open_short(candidate, state)`.
  - Calcula capital/notional.
  - Ejecuta SELL MARKET.
  - Coloca TP LIMIT reduceOnly.
  - Coloca SL STOP_MARKET si esta habilitado.
  - Retorna `pos`.

### Gestion de posicion

- LONG: `longs.manage_long(pos, state)`.
- SHORT: `shorts.manage_short(pos, state)`.
- Parciales:
  - LONG: `bot._check_partial_long(pos, state)`.
  - SHORT: `bot._check_partial_short(pos, state)`.
- Guardian:
  - `sl_guardian._run()`.

### Salida

- TP/SL por OCO long: `longs.manage_long`.
- SL long sin OCO: `longs._recolocar_oco`.
- TP/SL short por posicion cerrada en exchange: `shorts.manage_short`.
- SL short software: `shorts.manage_short` -> `_close_short_market`.
- Stale exits: `longs.manage_long`, `shorts.manage_short`.
- Cierre preventivo BTC: `bot._run()` tras `market.check_btc_momentum_close`.
- Guardian SL: `sl_guardian._run()`.
- Consolidacion normal: `bot._handle_close(...)`.

### Analytics

- Apertura LONG: `bot._safe_log_open(...)` despues de `state['positions'].append(pos)`.
- Apertura SHORT: `bot._safe_log_open(...)` despues de `state['positions'].append(pos)`.
- Decision snapshots: `bot._safe_log_decision_snapshot(...)` despues de evaluar scans LONG/SHORT.
- Cierres TP/SL/stale: `bot._safe_log_close(...)` desde `_handle_close`.
- Partial TP: llamadas directas a `AnalyticsLogger.log_trade_close(...)` con `trade_id:partial`.
- Guardian SL: `sl_guardian._run()` registra cierre con `trade_id` de la posicion.
- Circuit breaker: `AnalyticsLogger.log_event('CIRCUIT_BREAKER', ...)`.

## Inventario de indicadores

| Indicador | Donde se calcula | Que representa | Impacto en score/filtro | Lado |
|---|---|---|---|---|
| EMA20 4h BTC | `market.get_btc_context` | Tendencia macro BTC corta | Define `trend`; afecta modo direccional y contra tendencia | Ambos |
| EMA50 4h BTC | `market.get_btc_context` | Tendencia macro BTC media | Define `trend`; afecta modo direccional y contra tendencia | Ambos |
| ATR 4h BTC | `market.get_btc_context` | Volatilidad macro BTC | Contexto informativo | Ambos |
| Cambio BTC 4h | `market.get_btc_context`, `check_btc_momentum`, `check_btc_momentum_close` | Momentum macro | Pausa entradas, fuerza modo, cierre preventivo | Ambos |
| Cambio BTC 1h | `market.get_btc_context` | Rebote o impulso reciente | Penaliza shorts si supera `BTC_REBOUND_1H_PCT` | SHORT |
| EMA9 15m | `market.confirm_15m` | Momentum corto | Requisito de confirmacion; si falla sube score minimo | Ambos |
| EMA20 15m | `market.confirm_15m` | Momentum corto | Requisito de confirmacion; si falla sube score minimo | Ambos |
| RSI 15m | `market.confirm_15m` | Sobrecompra/sobreventa inmediata | Confirma o debilita entrada de corto plazo | Ambos |
| ATR 15m | `market.confirm_15m` | Volatilidad corta | Usado en confirmacion 15m | Ambos |
| MACD hist 15m | `market.confirm_15m` | Momentum corto | Parte de confirmacion 15m | Ambos |
| EMA20 1h | `score_long`, `score_short` | Tendencia local | LONG: +2 si precio arriba; SHORT: +2 si precio abajo | Ambos |
| EMA50 1h | `score_long`, `score_short` | Tendencia local media | LONG: +1 si precio arriba; SHORT: +1 si precio abajo | Ambos |
| RSI 1h | `score_long`, `score_short` | Sobrecompra/sobreventa | LONG: +2 zona ok o +1 sobrevendido; SHORT: +3 sobrecomprado o +1 rango valido | Ambos |
| MACD hist 1h | `score_long`, `score_short` | Momentum 1h | LONG: +2 si positivo; SHORT: +1 si negativo | Ambos |
| ATR 1h | `score_long`, `score_short` | Volatilidad local | Define SL/TP; `atr_pct` filtra min/max | Ambos |
| Volumen relativo 10 velas | `score_long`, `score_short` | Participacion reciente | LONG +1 si `vol_r > 1.1`; SHORT contribuye via distribucion | Ambos |
| Volumen relativo 24h | `score_long`, `score_short` | Liquidez reciente | Descarta si `< 0.5` | Ambos |
| EMA20 4h simbolo | `score_long`, `score_short` | Tendencia superior del simbolo | LONG: +1 si precio arriba; SHORT: +1 si precio abajo | Ambos |
| EMA50 4h simbolo | `score_short` | Tendencia superior media | SHORT: +1 si precio abajo | SHORT |
| RSI 4h simbolo | `score_short` | Sobrecompra 4h | SHORT: +1 si `rsi_4h > 60` | SHORT |
| MACD hist 4h | `score_short` | Momentum superior | SHORT: +1 si negativo | SHORT |
| Death cross | `score_short` | EMA20 cruza bajo EMA50 | SHORT: +2 | SHORT |
| Divergencia bajista RSI | `score_short` | Precio sube mientras RSI baja | SHORT: +2 | SHORT |
| Volumen en distribucion | `score_short` | Mas volumen en velas bajistas | SHORT: +1 | SHORT |
| Correlacion BTC | `score_long` | Dependencia del simbolo frente a BTC | Descarta long si alta corr y BTC debil | LONG |
| ATR expansion 7d | `score_long`, `score_short` | Volatilidad anormal vs promedio reciente | Sube `min_score` +3 | Ambos |
| Distancia a high 24h | `score_long` | Cercania a resistencia | Sube `min_score` +2 si resistencia esta cerca | LONG |
| Distancia a low 24h | `score_short` | Cercania a soporte | Sube `min_score` +2 si soporte esta cerca | SHORT |
| Rebote desde minimo | `_check_recovery_from_low` | Riesgo de short tras rebote | Sube `min_score` +2 o +3 | SHORT |
| Caida sostenida | `_check_oversold_for_short` | Riesgo de vender piso | Sube `min_score` +2 o +3 | SHORT |
| Rally sobreextendido | `_check_overbought_for_long` | Riesgo de comprar techo | Sube `min_score` +2 o +3 | LONG |
| Volatilidad horaria | `check_volatility_risk` | Riesgo de simbolo | Marca risky o auto-blacklist | Ambos |
| Rango 48h | `check_volatility_risk` | Riesgo extremo de rango | Marca risky o auto-blacklist | Ambos |

## Inventario de filtros

| Filtro | Archivo | Funcion | Condicion | Motivo | Evita |
|---|---|---|---|---|---|
| Lock de instancia | `bot.py` | `main` | Si no obtiene lock, sale | Evitar ejecuciones concurrentes | Doble gestion/ordenes duplicadas |
| Pausa global | `bot.py` | `_run` | `state.status == paused` | Respetar limite diario/circuit breaker | Nuevas entradas durante pausa |
| Circuit breaker | `bot.py` | `_run` | `consec_sl >= 4` | Cortar racha negativa | Sobreoperar tras SLs repetidos |
| Max posiciones abiertas | `bot.py` | `_run` | `len(active_positions) >= 3` | Limitar exposicion total | Sobrediversificacion |
| Cierre preventivo BTC | `bot.py` | `_run` | `check_btc_momentum_close` | Reducir riesgo ante pump/dump extremo | Mantener posiciones contra movimiento macro |
| Pausa post-SL | `bot.py` | `_run` | `skip_next_cycles > 0` | Enfriamiento tras SL | Reentrada inmediata |
| Exclusion activos abiertos | `bot.py` | `_run` | `active_symbols` en excluded | No duplicar simbolos | Doble posicion en mismo simbolo |
| Cooldown por simbolo | `utils.py`, `bot.py` | `get_active_cooldowns` | Simbolo en cooldown | Evitar reentrada tras SL | Revenge trading por par |
| Momentum BTC entrada | `market.py` | `check_btc_momentum` | `abs(change_4h) >= BTC_MOMENTUM_PAUSE_PCT` | Mercado extremo | Comprar top / vender piso |
| Modo direccional LONG | `market.py` | `scan_longs` | Bloquea longs en bearish | Seguir tendencia macro | Long contra BTC bajista |
| Modo direccional SHORT | `market.py` | `scan_shorts` | Bloquea shorts en bullish | Seguir tendencia macro | Short contra BTC alcista |
| Force mode LONG | `market.py` | `scan_longs` | `force_mode == short_only` | Movimiento extremo BTC | Longs durante crash |
| Force mode SHORT | `market.py` | `scan_shorts` | `force_mode == long_only` | Movimiento extremo BTC | Shorts durante pump |
| Blacklist estatica | `market.py` | `scan_longs`, `scan_shorts` | Simbolo en `BLACKLIST_SYMBOLS` | Excluir riesgo conocido | Microcaps / simbolos problematicos |
| Blacklist dinamica | `market.py` | `_load_dynamic_blacklist`, `_persist_blacklist` | Auto-blacklist por volatilidad | Persistir exclusiones | Repetir tokens peligrosos |
| Rehabilitacion blacklist | `market.py` | `review_dynamic_blacklist` | 48h y volatilidad/rango normalizados | Recuperar simbolos estables | Blacklist permanente innecesaria |
| Riesgo volatilidad | `market.py` | `check_volatility_risk` | Vol horaria/rango 48h alto | Detectar riesgo anormal | Spikes, manipulacion, slippage |
| Apertura mercado US | `market.py` | `score_long`, `score_short` | Token stock y primeros 45 min US | Evitar price discovery | Volatilidad de apertura |
| Confirmacion 15m | `market.py` | `score_long`, `score_short` | Si no confirma, sube score minimo | Evitar entrada sin momentum corto | Setups debiles |
| Volumen bajo | `market.py` | `score_long`, `score_short` | `vol_ratio < 0.5` | Liquidez insuficiente | Slippage/manipulacion |
| Rally sobreextendido long | `market.py` | `_check_overbought_for_long` | Rally > umbral desde minimo reciente | Evitar comprar techo | SL por agotamiento |
| Rebote short | `market.py` | `_check_recovery_from_low` | Rebote > umbral desde minimo | Evitar short contra rebote | SL por squeeze |
| Caida sostenida short | `market.py` | `_check_oversold_for_short` | Caida > umbral desde high reciente | Evitar vender piso | Rebote violento |
| ATR expansion | `market.py` | `score_long`, `score_short` | `atr_pct > atr_7d_avg * 2` | Volatilidad anormal | Entradas en expansion extrema |
| Resistencia cerca | `market.py` | `score_long` | `dist_to_high < 2%` | Evitar poco recorrido al alza | TP limitado/reversa |
| Soporte cerca | `market.py` | `score_short` | `dist_to_low < 2%` | Evitar poco recorrido a la baja | Rebote desde soporte |
| ATR minimo | `market.py` | `scan_longs`, `scan_shorts` | `atr_pct < ATR_MIN_PCT` | Mercado sin movimiento | Trades estancados |
| ATR maximo | `market.py` | `scan_longs`, `scan_shorts` | `atr_pct > ATR_MAX_PCT` | Volatilidad extrema | SL por ruido |
| RSI max long | `market.py` | `scan_longs` | `rsi > RSI_MAX_LONG` | Evitar sobrecompra | Comprar tarde |
| RSI min short | `market.py` | `scan_shorts` | `rsi < RSI_MIN_SHORT` | Evitar sobreventa | Vender tarde |
| Correlacion BTC long | `market.py` | `scan_longs` | `corr_btc > BTC_CORR_MAX` y BTC debil | Reducir contagio BTC | Long en alt correlacionada con BTC debil |
| Score insuficiente | `market.py` | `scan_longs`, `scan_shorts` | `score < effective_min` | Calidad minima de setup | Entradas mediocres |
| Counter-trend score | `market.py` | `scan_longs`, `scan_shorts` | Contra tendencia usa `SCORE_MIN_COUNTER` | Exigir mas calidad | Operar contra macro debil |
| Min qty spot | `longs.py` | `open_long` | `qty < min_qty` | Respetar exchange | Orden rechazada |
| Min notional spot | `longs.py` | `open_long` | `qty * price < min_notional` | Respetar exchange | Orden rechazada |
| OCO obligatorio long | `longs.py` | `open_long` | Si falla OCO, vende emergencia | No dejar spot sin SL/TP | Posicion desprotegida |
| Min qty futures | `shorts.py` | `open_short` | `qty < min_qty` | Respetar exchange | Orden rechazada |
| Min notional futures | `shorts.py` | `open_short` | `qty * price < min_notional` | Respetar exchange | Orden rechazada |
| SL min distance | `longs.py`, `shorts.py`, `market.py` | open/score | SL demasiado cerca | Evitar stops invalidos/ruido | SL inmediato |
| Native SL max distance | `shorts.py`, `bot.py` | `open_short`, partial short | SL no exceda aprox 4.5% mark | Evitar rechazo Binance | Orden STOP rechazada |
| Partial min notional long | `bot.py` | `_check_partial_long` | Parcial/resto bajo minimo | Evitar orden invalida | Venta/re-OCO fallido |
| Partial min qty short | `bot.py` | `_check_partial_short` | `qty_half < min_qty` | Evitar orden invalida | Cierre parcial rechazado |
| Stale max hours | `longs.py`, `shorts.py` | `manage_long`, `manage_short` | `elapsed_h > STALE_MAX_HOURS` | Liberar capital | Posiciones eternas |
| Stale low movement | `longs.py`, `shorts.py` | `manage_long`, `manage_short` | `elapsed_h > STALE_HOURS` y rango bajo | Liberar capital | Trades sin desplazamiento |
| Guardian long con OCO | `sl_guardian.py` | `_run` | Si long tiene OCO, no actua | Binance gestiona OCO | Doble cierre |
| Guardian short nativo | `sl_guardian.py` | `_run` | Si SL nativo ya cerro, solo limpia | Evitar doble cierre | Compra reduceOnly duplicada |
| Orphan cooldown | `bot.py` | `_audit_orphans` | Activo en cooldown no se protege automatico | Evitar reabrir cierre reciente | Recuperacion erronea |
| Orphan dust | `bot.py` | `_audit_orphans` | Polvo en limpieza se ignora si bajo valor | Evitar ruido por residuos | Posiciones artificiales |
| Rebalance minimo | `rebalance.py` | `rebalance` | Transferencia menor a minimo no se ejecuta | Evitar movimientos irrelevantes | Fees/ruido operativo |
| Wallet minima | `rebalance.py` | `rebalance` | No dejar wallet bajo minimo | Mantener operabilidad | Sin USDT para gestion |

## Inventario de parametros configurables

| Parametro | Archivo | Valor actual | Impacto |
|---|---|---|---|
| `API_KEY` | `config.py` via `config_loader.py` | `BINANCE_API_KEY` | Credencial Binance obligatoria para operar. |
| `API_SECRET` | `config.py` via `config_loader.py` | `BINANCE_API_SECRET` | Secreto Binance obligatorio para llamadas firmadas. |
| `SPOT_BASE` | `config.py` via `config_loader.py` | `BINANCE_SPOT_BASE` o `https://api.binance.com` | Endpoint spot. |
| `FUTURES_BASE` | `config.py` via `config_loader.py` | `BINANCE_FUTURES_BASE` o `https://fapi.binance.com` | Endpoint futures. |
| `BASE_DIR` | `config.py` | directorio `trading` | Base de rutas locales. |
| `STATE_FILE` | `config.py` via `config_loader.py` | `STATE_FILE` o `trading/state.json` | Persistencia operativa. |
| `TRADES_LOG` | `config.py` via `config_loader.py` | `TRADES_LOG` o `trading/trades_log.txt` | Log humano. |
| `ANALYSIS_LOG` | `config.py` via `config_loader.py` | `ANALYSIS_LOG` o `trading/analysis_log.txt` | Log de analisis de candidatos. |
| `LOCK_FILE` | `config.py` via `config_loader.py` | `LOCK_FILE` o `/tmp/trading_bot.lock` | Lock de ejecucion. |
| `ALERT_TARGET` | `config.py` via `config_loader.py` | `ALERT_TARGET` | Destino de alertas si se usa. |
| `DRY_RUN` | `config.py` | `False` | Simula sin ordenes si se activa. |
| `SPOT_RISK_PCT` | `config.py` | `0.93` | Capital spot usado por long. |
| `FUTURES_RISK_PCT` | `config.py` | `0.50` | Capital futures base por short. |
| `FUTURES_LEVERAGE` | `config.py` | `2` | Apalancamiento futures. |
| `SPOT_RISK_REDUCED` | `config.py` | `0.50` | Riesgo spot reducido tras SLs. |
| `MAX_CONSEC_SL` | `config.py` | `2` | Umbral para reducir riesgo. |
| `DIVERSIFY_THRESHOLD_1` | `config.py` | `30.0` | Capital para permitir mas posiciones. |
| `DIVERSIFY_THRESHOLD_2` | `config.py` | `50.0` | Capital para permitir hasta 3 posiciones. |
| `DIVERSIFY_RISK_2` | `config.py` | `0.45` | Riesgo por posicion al diversificar en 2. |
| `DIVERSIFY_RISK_3` | `config.py` | `0.30` | Riesgo por posicion al diversificar en 3. |
| `DUST_CLEAN_DAY` | `config.py` | `0` | Dia semanal de limpieza de polvo. |
| `DUST_MIN_VALUE_USD` | `config.py` | `0.10` | Minimo para convertir polvo. |
| `DUST_PROTECTED` | `config.py` | `{'USDT','USDC','BNB'}` | Activos nunca convertidos. |
| `MAX_LONG_POSITIONS` | `config.py` | `2` | Maximo de longs segun helpers. |
| `MAX_SHORT_POSITIONS` | `config.py` | `2` | Maximo de shorts segun helpers. |
| `SL_ATR_MULT` | `config.py` | `1.0` | Distancia SL long por ATR. |
| `SL_ATR_MULT_SHORT` | `config.py` | `1.5` | Distancia SL short por ATR. |
| `TP_ATR_MULT` | `config.py` | `2.0` | Distancia TP por ATR. |
| `SL_MIN_DIST_PCT` | `config.py` | `1.0` | Distancia minima SL. |
| `PARTIAL_TAKE_PCT` | `config.py` | `0.5` | Punto de TP parcial. |
| `BTC_MOMENTUM_PAUSE_PCT` | `config.py` | `2.0` | Pausa entradas por movimiento BTC 4h. |
| `BTC_MOMENTUM_CLOSE_PCT` | `config.py` | `4.0` | Cierra shorts por pump BTC. |
| `BTC_MOMENTUM_CLOSE_LONGS` | `config.py` | `-4.0` | Cierra longs por dump BTC. |
| `BTC_MOMENTUM_WINDOW_H` | `config.py` | `4` | Ventana informativa de momentum. |
| `DIRECTIONAL_MODE` | `config.py` | `True` | Bloquea trades contra tendencia BTC. |
| `DIRECTIONAL_NEUTRAL_BOTH` | `config.py` | `True` | Permite ambos lados en neutral. |
| `RSI_MAX_LONG` | `config.py` | `65` | Max RSI para long. |
| `RSI_MIN_SHORT` | `config.py` | `42` | Min RSI para short. |
| `ATR_MIN_PCT` | `config.py` | `0.5` | Volatilidad minima. |
| `ATR_MAX_PCT` | `config.py` | `3.5` | Volatilidad maxima. |
| `SCORE_MIN` | `config.py` | `5` | Score minimo base. |
| `SCORE_MIN_VOLATILE` | `config.py` | `6` | Score minimo si ATR 4h alto. |
| `SCORE_MIN_COUNTER` | `config.py` | `11` | Score minimo contra tendencia. |
| `ATR_VOLATILE_THRESH` | `config.py` | `3.0` | Umbral ATR 4h volatil. |
| `TRAIL_STEP_PCT` | `config.py` | `1.0` | Paso para mover trailing SL. |
| `NATIVE_SL_ENABLED` | `config.py` | `True` | Intenta STOP_MARKET nativo futures. |
| `RECOVERY_FROM_LOW_PCT` | `config.py` | `3.0` | Umbral rebote/caida para penalizaciones. |
| `RECOVERY_CONSEC_CANDLES` | `config.py` | `3` | Velas consecutivas para penalizar. |
| `DAILY_LOSS_LIMIT_PCT` | `config.py` | `5.0` | Pausa por perdida diaria. |
| `STALE_HOURS` | `config.py` | `5` | Tiempo para stale por bajo movimiento. |
| `STALE_RANGE_PCT` | `config.py` | `0.5` | Rango maximo para stale. |
| `STALE_MAX_HOURS` | `config.py` | `12` | Cierre maximo por antiguedad. |
| `COOLDOWN_AFTER_SL` | `config.py` | `True` | Activa cooldown tras SL. |
| `COOLDOWN_HOURS` | `config.py` | `8` | Duracion cooldown. |
| `BNB_FEE_RATE` | `config.py` | `0.00075` | Fee spot estimado. |
| `FUTURES_FEE_RATE` | `config.py` | `0.0004` | Fee futures estimado. |
| `OCO_MAX_RETRIES` | `config.py` | `3` | Reintentos al colocar OCO. |
| `BTC_CORR_MAX` | `config.py` | `0.85` | Max correlacion BTC en long con BTC debil. |
| `BTC_WEAK_PCT` | `config.py` | `-0.5` | BTC debil para correlacion. |
| `BTC_STRONG_PCT` | `config.py` | `0.5` | BTC fuerte. |
| `BTC_REBOUND_1H_PCT` | `config.py` | `0.3` | Penalizacion shorts por rebote BTC. |
| `BTC_CRASH_PCT` | `config.py` | `-5.0` | Fuerza modo `short_only`. |
| `BTC_PUMP_PCT` | `config.py` | `5.0` | Fuerza modo `long_only`. |
| `BLACKLIST_SYMBOLS` | `config.py` | set actual | Excluye simbolos permanentes. |
| `US_STOCK_TOKENS` | `config.py` | set actual | Identifica tokenized stocks. |
| `US_MARKET_OPEN_UTC` | `config.py` | `(14, 30)` | Apertura US usada por filtro. |
| `US_MARKET_AVOID_MIN` | `config.py` | `45` | Ventana de bloqueo tras apertura. |
| `RISKY_SYMBOLS` | `config.py` | set actual | Simbolos de riesgo especial. |
| `RISKY_SCORE_BONUS` | `config.py` | `2` | Sube score requerido para risky. |
| `RISKY_RISK_FACTOR` | `config.py` | `0.50` | Reduce capital en risky. |
| `RISKY_VOL_HOURLY_MAX` | `config.py` | `4.0` | Auto-blacklist por volatilidad horaria. |
| `RISKY_RANGE_48H_MAX` | `config.py` | `60.0` | Auto-blacklist por rango 48h. |
| `SPOT_RISK_BEARISH` | `config.py` | `0.50` | Reduce riesgo long en contexto bajista. |
| `NET_RETRIES` | `config.py` | `3` | Reintentos de red. |
| `NET_RETRY_DELAY` | `config.py` | `2.0` | Delay entre reintentos. |
| `RATIO_BEARISH_FUTURES` | `rebalance.py` | `0.65` | Objetivo futures en bearish. |
| `RATIO_VERY_BEARISH_FUTURES` | `rebalance.py` | `0.80` | Objetivo futures en bearish persistente. |
| `VERY_BEARISH_DAYS` | `rebalance.py` | `3.0` | Dias para very bearish. |
| `RATIO_BULLISH_SPOT` | `rebalance.py` | `0.65` | Objetivo spot en bullish. |
| `RATIO_VERY_BULLISH_SPOT` | `rebalance.py` | `0.80` | Objetivo spot en bullish persistente. |
| `VERY_BULLISH_DAYS` | `rebalance.py` | `3.0` | Dias para very bullish. |
| `REBALANCE_MIN_USDT` | `rebalance.py` | `2.0` | Transferencia minima. |
| `REBALANCE_MIN_WALLET` | `rebalance.py` | `3.0` | Saldo minimo por wallet. |
| `ANALYTICS_FILE` | `analytics.py` via `config_loader.py` | `ANALYTICS_FILE` o `trading/trade_analytics.jsonl` | Ruta de eventos estructurados. |
| `DECISION_SNAPSHOTS_FILE` | `analytics.py` via `config_loader.py` | `DECISION_SNAPSHOTS_FILE` o `trading/decision_snapshots.jsonl` | Ruta de snapshots de decision. |
| `CSV_FILE` | `analytics.py` via `config_loader.py` | `CSV_FILE` o `trading/reports/trades.csv` | Ruta de export CSV. |

## Riesgos tecnicos detectados

- Si `.env` no existe o faltan `BINANCE_API_KEY`/`BINANCE_API_SECRET`, el bot no debe operar; `setup_check.py` lo reporta antes del despliegue.
- `bot.py` define `MAX_OPEN_POSITIONS = 3` localmente, separado de `MAX_LONG_POSITIONS` y `MAX_SHORT_POSITIONS`.
- El cierre preventivo de BTC calcula PnL y remueve posiciones; revisar en fase futura que efectivamente ejecute orden de cierre de mercado en todos los lados antes de remover.
- En cierre preventivo long se consulta `pos.get('oco_id')`, mientras las posiciones long usan `oco_order_list_id`.
- `trade_analytics.jsonl` es append-only; si se repite un evento con mismo `trade_id`, export conserva el ultimo snapshot.
- `volume_ratio`, `ema20`, `ema50`, `macd_hist`, `atr_pct` y `btc_correlation` se exponen en candidatos cuando el scoring los calcula; rutas sin candidato o sin calculo conservan `null`.
