# Module Guide

Guia de alto nivel de los modulos principales.

## `trading/bot.py`

**Proposito:** orquestador principal del ciclo.

**Responsabilidades:** lock, carga/guardado de estado, reset diario, circuit breaker, auditoria de orphans, rebalance, gestion de posiciones, scans, aperturas, cierres, analytics y snapshot observable.

**Consume:** `config`, `utils`, `market`, `longs`, `shorts`, `rebalance`, `capital_manager`, `bot_state`, `analytics`.

**Lo usan:** systemd/manual run.

## `trading/capital_manager.py`

**Proposito:** fuente de verdad de guardrails de capital.

**Responsabilidades:** leer limites, calcular capital usable, calcular max margin por posicion, validar orden Spot/Futures, limitar transferencias y producir snapshot de capital.

**Consume:** variables de entorno y estado de posiciones.

**Lo usan:** `longs.py`, `shorts.py`, `bot.py`, `bot_state.py`, dashboard.

## `trading/rebalance.py`

**Proposito:** asignar capital entre Spot y Futures segun regimen.

**Responsabilidades:** calcular targets, detectar tendencia persistente, respetar reserva opcional, calcular transferencias y ejecutar universal transfer.

**Consume:** `utils`, `config`, `state`, contexto BTC.

**Lo usan:** `bot.py`, `bot_state.py`.

## `trading/longs.py`

**Proposito:** abrir y gestionar posiciones Long Spot.

**Responsabilidades:** validar capital/capacidad, BUY MARKET, OCO TP/SL, retry OCO, emergency sell, recovery pending, trailing, stale exit y recolocacion OCO.

**Consume:** `utils`, `config`, `capital_manager`, filtros Binance Spot.

**Lo usan:** `bot.py`.

## `trading/shorts.py`

**Proposito:** abrir y gestionar posiciones Short Futures.

**Responsabilidades:** leverage, SELL MARKET, TP reduceOnly, SL nativo/software, trailing, stale exit, cierre market y cancelacion de TP.

**Consume:** `utils`, `config`, `capital_manager`, filtros Binance Futures.

**Lo usan:** `bot.py`.

## `trading/sl_guardian.py`

**Proposito:** proteccion independiente de SL.

**Responsabilidades:** revisar posiciones abiertas, cerrar Longs sin OCO que tocan SL usando balance real, cerrar Shorts segun SL, actualizar estado y registrar analytics.

**Consume:** `utils`, `config`, `analytics`, `state.json`.

**Lo usan:** systemd/manual guardian.

## `trading/telegram_commands.py`

**Proposito:** interfaz Telegram read-only.

**Responsabilidades:** recibir updates, validar chat autorizado, renderizar paginas, responder comandos/callbacks y guardar offset.

**Consume:** `bot_state.json`, `state.json`, JSONL, `config_loader`, systemd status.

**Lo usan:** servicio Telegram.

## `trading/telegram_alerts.py`

**Proposito:** envio de alertas Telegram configurables.

**Responsabilidades:** resolver flags `TELEGRAM_NOTIFY_*`, aplicar cooldown basico y enviar mensajes.

**Consume:** variables de entorno y Telegram API.

**Lo usan:** flujos de alerta via `utils` y modulos operativos.

## `trading/config.py`

**Proposito:** configuracion central del bot.

**Responsabilidades:** exponer credenciales desde loader, rutas, parametros de riesgo, filtros, scoring, TP/SL, cooldown, blacklist y red.

**Consume:** `config_loader.load_config(require_api=True)`.

**Lo usan:** modulos de trading live.

## `trading/config_loader.py`

**Proposito:** cargar `.env` y variables sin acoplar herramientas read-only a credenciales obligatorias.

**Responsabilidades:** parsear variables, construir rutas absolutas y permitir `require_api=False`.

**Consume:** `.env` raiz o `trading/.env`.

**Lo usan:** `config.py`, dashboard, Telegram, setup/tools.

## `trading/market.py`

**Proposito:** contexto de mercado, scoring y seleccion de candidatos.

**Responsabilidades:** obtener contexto BTC, calcular score Long/Short, aplicar filtros, blacklist dinamica, candidatos dinamicos y snapshots de decision.

**Consume:** `utils`, `config`, datos publicos Binance.

**Lo usan:** `bot.py`.

## `trading/analytics.py`

**Proposito:** telemetria estructurada.

**Responsabilidades:** log de opens/closes/events, decision snapshots, merge de trades, export CSV y puente pasivo hacia `history.py`.

**Consume:** runtime config y archivos JSONL.

**Lo usan:** `bot.py`, `sl_guardian.py`, analizadores.

## `trading/history.py`

**Proposito:** memoria historica append-only para aprendizaje futuro.

**Responsabilidades:** registrar aperturas, cierres, decisiones y snapshots en `data/history/*.jsonl`; reconstruir un trade por `trade_id`; tolerar archivos inexistentes o lineas JSON invalidas sin romper el bot.

**Consume:** eventos pasivos desde `analytics.py`.

**Lo usan:** `analytics.py`, tests y futuras herramientas offline. No lo usa la estrategia para decidir.

## `trading/analytics_engine.py`

**Proposito:** motor analitico pasivo e indice precalculado.

**Responsabilidades:** reconstruir `data/history/stats.json` desde `trades.jsonl`, `decisions.jsonl` y `snapshots.jsonl`; actualizar agregados ante cierres nuevos; exponer getters de estadisticas generales, por simbolo, direccion, motivo de salida y tiempo.

**Consume:** `data/history/*.jsonl`.

**Lo usan:** actualmente tests y `analytics.py` para update incremental pasivo. En futuras iteraciones lo leeran Telegram y Dashboard. No lo usa la estrategia para decidir.

## `trading/utils.py`

**Proposito:** capa compartida de infraestructura.

**Responsabilidades:** HTTP Binance, firma de requests, balances, precios, filtros, indicadores, locks, load/save state, cooldowns, logs, alertas y diagnostico HTTP.

**Consume:** `config`.

**Lo usan:** casi todos los modulos de trading.

## `trading/healthcheck.py`

**Proposito:** salud local sin tocar Binance.

**Responsabilidades:** validar `state.json`, lock, edades de archivos, analytics open trades y alineacion basica.

**Consume:** archivos locales y config read-only.

**Lo usan:** manual, `preflight_check.py`.

## `dashboard/app.py`

**Proposito:** dashboard local read-only.

**Responsabilidades:** servir UI estatica y endpoints `/api/status`, `/api/trades`, `/api/snapshots`, `/api/health`, `/api/metrics`.

**Consume:** `bot_state.json`, JSONL, analytics y config read-only.

**Lo usan:** navegador local/servicio dashboard.
