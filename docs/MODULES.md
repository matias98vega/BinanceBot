# Module Guide

Guia de alto nivel de los modulos principales.

## `trading/bot.py`

**Proposito:** orquestador principal del ciclo.

**Responsabilidades:** lock, carga/guardado de estado, reset diario, circuit breaker, rebalance, gestion de posiciones, scans, aperturas, timeline y coordinacion general. Mantiene wrappers de compatibilidad para cierres/parciales, auditoria y persistencia extraidos.

**Consume:** `config`, `utils`, `market`, `longs`, `shorts`, `rebalance`, `capital_manager`, `position_lifecycle`, `audit_pipeline`, `persistence_pipeline`, `analytics`, `decision_timeline`.

**Lo usan:** systemd/manual run.

## `trading/position_lifecycle.py`

**Proposito:** concentrar operaciones de lifecycle que antes vivian dentro de `bot.py`.

**Responsabilidades:** cierre contable/observable de posiciones, parciales Long/Short, recovery OCO auxiliar y rebalance post-cierre. No decide entradas ni cambia parametros de TP/SL.

**Consume:** `config`, `utils`, `rebalance`, `decision_timeline`.

**Lo usan:** `bot.py` mediante wrappers compatibles.

## `trading/audit_pipeline.py`

**Proposito:** aislar auditorias pasivas de wallet Spot y reconciliacion de orphans.

**Responsabilidades:** detectar activos Spot sin posicion local, intentar protegerlos con OCO usando la logica existente, reportar fallos y coordinar limpieza de polvo.

**Consume:** `config`, `utils`, cliente Binance inyectado desde `bot.py`.

**Lo usan:** `bot.py`.

## `trading/persistence_pipeline.py`

**Proposito:** centralizar persistencia segura de observabilidad del ciclo.

**Responsabilidades:** log de apertura/cierre hacia analytics, snapshot de decisiones y persistencia segura de BotState.

**Consume:** `bot_state`, `market`, `config`.

**Lo usan:** `bot.py`.

## `trading/capital_manager.py`

**Proposito:** fuente de verdad de guardrails de capital.

**Responsabilidades:** leer limites, calcular capital usable, calcular max margin por posicion, validar orden Spot/Futures, limitar transferencias y producir snapshot de capital.

**Consume:** variables de entorno, estado de posiciones y `decision_timeline`.

**Lo usan:** `longs.py`, `shorts.py`, `bot.py`, `bot_state.py`, dashboard.

## `trading/rebalance.py`

**Proposito:** asignar capital entre Spot y Futures segun regimen.

**Responsabilidades:** calcular targets, detectar tendencia persistente, respetar reserva opcional, calcular transferencias y ejecutar universal transfer.

**Consume:** `utils`, `config`, `state`, contexto BTC y `decision_timeline`.

**Lo usan:** `bot.py`, `bot_state.py`.

## `trading/longs.py`

**Proposito:** abrir y gestionar posiciones Long Spot.

**Responsabilidades:** validar capital/capacidad, BUY MARKET, OCO TP/SL, retry OCO, emergency sell, recovery pending, trailing, stale exit y recolocacion OCO.

**Consume:** `binance_client`, `utils`, `config`, `capital_manager`, `decision_timeline`, filtros Binance Spot.

**Lo usan:** `bot.py`.

## `trading/shorts.py`

**Proposito:** abrir y gestionar posiciones Short Futures.

**Responsabilidades:** leverage, SELL MARKET, TP reduceOnly, SL nativo/software, trailing, stale exit, cierre market y cancelacion de TP.

**Consume:** `binance_client`, `utils`, `config`, `capital_manager`, `decision_timeline`, filtros Binance Futures.

**Lo usan:** `bot.py`.

## `trading/sl_guardian.py`

**Proposito:** proteccion independiente de SL.

**Responsabilidades:** revisar posiciones abiertas, cerrar Longs sin OCO que tocan SL usando balance real, cerrar Shorts segun SL, actualizar estado, registrar timeline y analytics.

**Consume:** `utils`, `config`, `analytics`, `decision_timeline`, `state.json`.

**Lo usan:** systemd/manual guardian.

## `trading/telegram_commands.py`

**Proposito:** interfaz Telegram read-only.

**Responsabilidades:** recibir updates, validar chat autorizado, renderizar paginas, responder comandos/callbacks, mostrar estadisticas desde `analytics_engine`, insights desde `insights_engine`, timeline desde `decision_timeline`, inspeccion historica de trades desde `trade_inspector` y guardar offset.

**Consume:** `bot_state.json`, `state.json`, JSONL, `analytics_engine`, `insights_engine`, `decision_timeline`, `trade_inspector`, `config_loader`, systemd status.

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

**Consume:** `binance_client`, `utils`, `config`, datos publicos Binance.

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

## `trading/feature_store.py`

**Proposito:** persistencia rica y append-only de features por trade abierto.

**Responsabilidades:** registrar identificacion, mercado, indicadores del simbolo, scoring, capital, estado del bot y contexto de decision en `data/history/features.jsonl`; sanitizar secretos; ignorar `NaN`; tolerar errores de escritura sin romper el ciclo.

**Consume:** datos ya presentes en `analytics.py` durante la apertura de trade.

**Lo usan:** `analytics.py` y tests. Es base futura para Shadow Mode, Auto Optimizer, Replay, RL e IA, pero actualmente no participa en decisiones operativas.

## `trading/analytics_engine.py`

**Proposito:** motor analitico pasivo e indice precalculado.

**Responsabilidades:** reconstruir `data/history/stats.json` desde `trades.jsonl`, `decisions.jsonl` y `snapshots.jsonl`; actualizar agregados ante cierres nuevos; exponer getters de estadisticas generales, por simbolo, direccion, motivo de salida y tiempo.

**Consume:** `data/history/*.jsonl`.

**Lo usan:** actualmente tests y `analytics.py` para update incremental pasivo. En futuras iteraciones lo leeran Telegram y Dashboard. No lo usa la estrategia para decidir.

## `trading/insights_engine.py`

**Proposito:** motor pasivo de conclusiones automaticas.

**Responsabilidades:** leer estadisticas exclusivamente mediante `analytics_engine`/`stats.json`, generar `data/history/insights.json`, agrupar conclusiones por general, rendimiento, riesgo, simbolos, direccion, regimen, temporal y salidas, y exponer getters de insights para Telegram/dashboard.

**Consume:** `data/history/stats.json`.

**Lo usan:** Telegram, dashboard y tests. No lee `trades.jsonl`, no recalcula estadisticas base y no alimenta decisiones operativas.

## `trading/decision_timeline.py`

**Proposito:** linea cronologica compacta de decisiones y eventos observables.

**Responsabilidades:** registrar eventos JSONL con `event_id`, timestamp, nivel, categoria, simbolo/direccion opcional, mensaje, detalles sanitizados y trade relacionado; leer eventos recientes con filtros; compactar eventos para Telegram; rotar `data/history/timeline.jsonl` al superar 5 MB preservando eventos recientes.

**Consume:** `data/history/timeline.jsonl`.

**Lo usan:** `bot.py`, `rebalance.py`, `longs.py`, `shorts.py`, `capital_manager.py`, `sl_guardian.py`, `analytics.py`, `history.py`, Telegram, dashboard y tests. No alimenta estrategia ni sizing.

## `trading/trade_inspector.py`

**Proposito:** reconstruir la historia completa de un trade desde datos historicos locales.

**Responsabilidades:** buscar trades por id o simbolo/fecha cercana, reconstruir resumen, mercado, capital, protecciones, timeline relevante y conclusion deterministica; tolerar datos incompletos y lineas corruptas.

**Consume:** `data/history/trades.jsonl`, `decisions.jsonl`, `snapshots.jsonl`, `timeline.jsonl` y `analytics_engine`.

**Lo usan:** Telegram, dashboard y tests. No consulta Binance, no depende de `state.json` y no participa en decisiones operativas.

## `trading/binance_client.py`

**Proposito:** punto unico e inyectable de acceso a Binance.

**Responsabilidades:** exponer metodos de alto nivel para precios, cuentas, ordenes Spot/Futures, OCO, cancelaciones, transferencias y exchange info; delegar 1:1 en `utils` sin cambiar firma, autenticacion, payloads, errores, retries ni logging.

**Consume:** `utils`.

**Lo usan:** modulos operativos que necesitan Binance. La arquitectura queda preparada para futuros `FakeBinanceClient`, `ReplayBinanceClient`, `PaperBinanceClient` y `ShadowBinanceClient`.

## `trading/utils.py`

**Proposito:** capa compartida de infraestructura.

**Responsabilidades:** HTTP Binance, firma de requests, balances, precios, filtros, indicadores, locks, load/save state, cooldowns, logs, alertas y diagnostico HTTP.

**Consume:** `config`.

**Lo usan:** `binance_client.py` para acceso al exchange y otros modulos para helpers no relacionados con Binance.

## `trading/healthcheck.py`

**Proposito:** salud local sin tocar Binance.

**Responsabilidades:** validar `state.json`, lock, edades de archivos, analytics open trades y alineacion basica.

**Consume:** archivos locales y config read-only.

**Lo usan:** manual, `preflight_check.py`.

## `dashboard/app.py`

**Proposito:** dashboard local read-only.

**Responsabilidades:** servir UI estatica y endpoints `/api/status`, `/api/trades`, `/api/snapshots`, `/api/health`, `/api/metrics`, `/api/timeline`, `/api/insights`, `/api/trade/<id>`.

**Consume:** `bot_state.json`, JSONL, analytics, `decision_timeline`, `insights_engine`, `trade_inspector` y config read-only.

**Lo usan:** navegador local/servicio dashboard.
