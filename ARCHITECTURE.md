# BinanceBot Architecture

Este documento es la referencia canonica de arquitectura. Describe el estado actual sin proponer cambios de estrategia.

## Vista General

```text
Binance API
   |
   v
trading/binance_client.py
   |
   v
trading/utils.py  <--------------+
   |                              |
   +--> market.py                 |
   +--> longs.py                  |
   +--> shorts.py                 |
   +--> rebalance.py              |
   +--> sl_guardian.py            |
                                  |
bot.py --------------------------+
   |
   +--> state.json
   +--> bot_state.json
   +--> trade_analytics.jsonl
   +--> decision_snapshots.jsonl
   +--> data/history/*.jsonl
   +--> data/history/features.jsonl
   +--> data/history/stats.json
   +--> data/history/insights.json
   +--> data/history/timeline.jsonl
   |
   +--> Telegram read-only
   +--> Dashboard read-only
```

## Flujo Completo de un Ciclo

```text
systemd timer / manual run
   |
   v
bot.py main()
   |
   +-- acquire lock
   +-- load state.json
   +-- daily reset / pause checks / circuit breaker
   +-- audit orphan Spot assets
   +-- review dynamic blacklist
   +-- get BTC context
   +-- rebalance Spot/Futures
   +-- manage existing positions
   +-- scan Long candidates
   +-- scan Short candidates
   +-- open accepted positions
   +-- write analytics, timeline events and decision snapshots
   +-- build/persist bot_state.json
   +-- save state.json
   +-- release lock
```

El ciclo principal vive en `trading/bot.py`. Su responsabilidad es orquestar; las decisiones de mercado viven en `market.py`, la ejecucion Long en `longs.py`, la ejecucion Short en `shorts.py`, los guardrails en `capital_manager.py`, el lifecycle de cierres/parciales en `position_lifecycle.py`, la auditoria Spot en `audit_pipeline.py` y la observabilidad persistente en `persistence_pipeline.py`/`analytics.py`/`decision_timeline.py`/`bot_state.py`.

## Flujo de Apertura

### Long Spot

```text
market.scan_longs()
   |
   v
longs.open_long()
   |
   +-- calcular capital permitido
   +-- validar capacity y capital_manager
   +-- leer filtros Spot
   +-- BUY MARKET
   +-- consultar balance real del asset
   +-- ajustar qty por stepSize/minQty/minNotional
   +-- crear OCO SELL TP/SL
   +-- si OCO -2010: recalcular balance real y retry una vez
   +-- si sigue fallando: emergency sell con balance real
   +-- si no se puede vender: recovery_pending
```

### Short Futures

```text
market.scan_shorts()
   |
   v
shorts.open_short()
   |
   +-- calcular margen permitido
   +-- validar capacity y capital_manager
   +-- asegurar leverage
   +-- leer filtros Futures
   +-- SELL MARKET
   +-- colocar TP LIMIT reduceOnly
   +-- colocar SL STOP_MARKET si aplica
   +-- persistir datos de proteccion
```

## Flujo de Cierre

```text
bot.py gestiona posicion
   |
   +-- longs.manage_long()
   |      +-- detecta OCO cerrado, stale, trailing o recovery
   |
   +-- shorts.manage_short()
          +-- detecta TP/SL exchange, stale, trailing o cierre software

bot._handle_close() -> position_lifecycle.handle_close()
   |
   +-- calcula/recibe PnL
   +-- actualiza daily/total PnL
   +-- registra cooldown si corresponde
   +-- escribe trades_log y analytics
   +-- remueve posicion de state
```

Los cierres parciales se coordinan desde `bot.py` mediante wrappers de compatibilidad, pero la logica vive en `position_lifecycle.py`.

## Flujo Guardian

```text
systemd guardian timer / manual run
   |
   v
sl_guardian.py
   |
   +-- load state.json
   +-- para Long:
   |      +-- si tiene OCO, no actua
   |      +-- si toca SL, consulta balance real
   |      +-- vende min(qty_estado, balance_libre)
   |      +-- si -2010 y balance=0, limpia como already_closed
   |      +-- si -2010 y balance>0, alerta CRITICAL
   |
   +-- para Short:
          +-- verifica precio/SL y cierra reduceOnly si aplica
```

Guardian no escanea nuevas entradas. Solo protege posiciones existentes.

## Flujo Rebalance

```text
bot.py obtiene btc_ctx
   |
   v
rebalance.rebalance(state, btc_ctx)
   |
   +-- calcula tendencia actual
   +-- calcula targets Spot/Futures
   +-- respeta posiciones abiertas
   +-- calcula monto transferible
   +-- aplica REBALANCE_MIN_USDT
   +-- aplica REBALANCE_MIN_WALLET_USDT opcional
   +-- ejecuta universal transfer si corresponde
```

Rebalance no abre ni cierra trades. Solo mueve USDT entre wallets.

## Flujo Telegram

```text
telegram_commands.py
   |
   +-- lee updates Telegram autorizados
   +-- despacha comando o callback
   +-- lee bot_state.json, state.json y JSONL locales
   +-- renderiza pagina read-only
   +-- guarda telegram_offset.json
```

Telegram no modifica `state.json`, no abre ordenes y no cierra ordenes. Las alertas salientes se centralizan en `telegram_alerts.py` y respetan variables `TELEGRAM_NOTIFY_*`.

La seccion `Estadisticas` de Telegram lee exclusivamente `data/history/stats.json` mediante `analytics_engine.py`. Si el indice no existe o esta corrupto, el engine lo reconstruye desde JSONL; Telegram no consulta `trades.jsonl` directamente.

La seccion `Insights` de Telegram lee conclusiones desde `insights_engine.py`. El motor de insights consume `stats.json` mediante `analytics_engine` y no lee `trades.jsonl` ni participa en decisiones.

La pagina `Timeline` y el comando `/timeline` leen exclusivamente `data/history/timeline.jsonl` mediante `decision_timeline.py`. Soporta filtros simples por categoria o simbolo y no envia notificaciones por evento.

La seccion `Inspeccionar Trade` y el endpoint `/api/trade/<id>` usan `trade_inspector.py` para reconstruir un trade desde `data/history/*.jsonl`, `timeline.jsonl` y `analytics_engine`. No consulta Binance, no depende de `state.json` y no participa en entradas/salidas.

`feature_store.py` registra un vector rico por trade abierto en `data/history/features.jsonl`. Es una base pasiva para Shadow Mode, Auto Optimizer, Replay, RL e IA futura; actualmente no participa en estrategia, scoring, sizing, TP/SL ni ordenes.

## Flujo Healthcheck

```text
healthcheck.py
   |
   +-- valida state.json
   +-- revisa lock
   +-- revisa edad de analytics/snapshots/logs
   +-- compara posiciones state vs analytics open

preflight_check.py
   |
   +-- healthcheck
   +-- validate_observability
   +-- analyze_trades
   +-- analyze_decisions

post_cycle_check.py
   |
   +-- baseline antes del ciclo
   +-- comparacion despues del ciclo
```

Estos scripts no abren ordenes ni cambian estrategia.

## Modulos Principales

| Modulo | Proposito | Consume | Usado por |
|---|---|---|---|
| `bot.py` | Orquestar ciclo principal y mantener wrappers de compatibilidad | `config`, `utils`, `binance_client`, `market`, `longs`, `shorts`, `rebalance`, `capital_manager`, `position_lifecycle`, `audit_pipeline`, `persistence_pipeline` | systemd/manual |
| `position_lifecycle.py` | Cierres, parciales y recovery OCO coordinados por el ciclo | `config`, `utils`, `rebalance`, `decision_timeline` | `bot.py` |
| `audit_pipeline.py` | Auditoria Spot, deteccion/reconciliacion de orphans y limpieza de polvo | `config`, `utils`, cliente Binance inyectado | `bot.py` |
| `persistence_pipeline.py` | Persistencia segura de BotState y logs pasivos de analytics/snapshots | `bot_state`, `market`, `config` | `bot.py` |
| `capital_manager.py` | Guardrails de capital, max margin por posicion, validacion y snapshot | env, estado | `bot.py`, `longs.py`, `shorts.py`, dashboard |
| `rebalance.py` | Mover USDT entre Spot/Futures segun regimen | `binance_client`, `utils`, `config`, `state` | `bot.py`, `bot_state.py` |
| `longs.py` | Apertura y gestion Long Spot | `binance_client`, `utils`, `config`, `capital_manager` | `bot.py`, recovery |
| `shorts.py` | Apertura y gestion Short Futures | `binance_client`, `utils`, `config`, `capital_manager` | `bot.py` |
| `sl_guardian.py` | Proteccion independiente de SL | `binance_client`, `utils`, `config`, `analytics` | systemd/manual |
| `market.py` | Contexto BTC, scoring, filtros y candidatos | `binance_client`, `utils`, `config` | `bot.py` |
| `binance_client.py` | Punto unico inyectable de acceso a Binance | `utils` | `bot.py`, `longs.py`, `shorts.py`, `rebalance.py`, `sl_guardian.py`, `market.py`, tests |
| `utils.py` | Implementacion HTTP Binance, firma, balances, filtros, indicadores, state, logs, alerts | `config` | `binance_client.py` y helpers internos |
| `config.py` | Constantes de estrategia/riesgo y runtime config | `config_loader` | modulos de trading |
| `config_loader.py` | Cargar `.env` y rutas sin requerir API en herramientas read-only | `.env` | `config`, dashboard, Telegram, tools |
| `analytics.py` | Eventos JSONL, snapshots, export CSV y puente hacia historia pasiva | runtime config | `bot.py`, guardian, analizadores |
| `history.py` | Memoria historica pasiva de trades, decisiones y snapshots | JSONL en `data/history/` | `analytics.py`, tests, futuras herramientas offline |
| `feature_store.py` | Persistencia rica de features por trade abierto | `data/history/features.jsonl` | `analytics.py`, tests, futuras herramientas de aprendizaje |
| `analytics_engine.py` | Indice estadistico pasivo precalculado desde historia JSONL | `data/history/*.jsonl`, `stats.json` | futuras consultas Telegram/dashboard, tests |
| `insights_engine.py` | Conclusiones pasivas derivadas de estadisticas | `data/history/stats.json`, `insights.json` | Telegram, dashboard, tests |
| `decision_timeline.py` | Timeline cronologico de decisiones y eventos operativos | `data/history/timeline.jsonl` | `bot.py`, `rebalance.py`, `longs.py`, `shorts.py`, `capital_manager.py`, `sl_guardian.py`, Telegram, dashboard |
| `trade_inspector.py` | Reconstruccion historica pasiva de un trade | `data/history/*.jsonl`, `timeline.jsonl`, `analytics_engine` | Telegram, dashboard, tests |
| `bot_state.py` | Snapshot observable de estado/capital/sistema | `state`, `capital_manager`, `rebalance`, systemd | `bot.py`, Telegram, dashboard |
| `telegram_commands.py` | Menu y comandos read-only | `bot_state`, JSONL, `state`, `analytics_engine` | servicio Telegram |
| `telegram_alerts.py` | Alertas configurables por tipo | env, Telegram API | `utils`/flujos de alerta |
| `healthcheck.py` | Salud local del estado y observabilidad | archivos locales | preflight/manual |
| `dashboard/app.py` | API y UI local read-only | `bot_state`, JSONL, analytics | servicio dashboard |

## Almacenamiento Local

| Archivo | Tipo | Rol |
|---|---|---|
| `trading/state.json` | JSON mutable | Fuente operativa de posiciones abiertas y acumuladores |
| `trading/bot_state.json` | JSON mutable | Snapshot de lectura para Telegram/dashboard |
| `trading/trades_log.txt` | texto append-only | Log humano historico |
| `trading/trade_analytics.jsonl` | JSONL append-only | Eventos estructurados de trades |
| `trading/decision_snapshots.jsonl` | JSONL append-only | Snapshots de decisiones por ciclo |
| `data/history/trades.jsonl` | JSONL append-only | Historia normalizada de aperturas/cierres |
| `data/history/decisions.jsonl` | JSONL append-only | Contexto explicativo de decisiones |
| `data/history/snapshots.jsonl` | JSONL append-only | Contexto de mercado/capital |
| `data/history/features.jsonl` | JSONL append-only | Feature Store pasivo para aprendizaje futuro |
| `data/history/stats.json` | JSON derivado | Indice precalculado de estadisticas; se puede reconstruir |
| `data/history/insights.json` | JSON derivado | Conclusiones y alertas pasivas generadas desde `stats.json` |
| `data/history/timeline.jsonl` | JSONL rotado | Timeline cronologico de eventos de decision y observabilidad |
| `trading/.cycle_baseline.json` | JSON | Baseline local pre/post ciclo |
| `trading/telegram_offset.json` | JSON | Offset de updates Telegram |
| `trading/blacklist_dynamic.json` | JSON | Blacklist dinamica persistida |

## Observabilidad

La observabilidad actual tiene tres capas:

1. **Estado actual:** `state.json` y `bot_state.json`.
2. **Historico estructurado:** `trade_analytics.jsonl` y `decision_snapshots.jsonl`.
3. **Memoria historica pasiva:** `data/history/trades.jsonl`, `data/history/decisions.jsonl`, `data/history/snapshots.jsonl`.
4. **Feature Store:** `data/history/features.jsonl` con features de mercado, indicadores, scoring, capital, estado del bot y contexto de decision por apertura.
5. **Decision Timeline:** `data/history/timeline.jsonl` para eventos compactos por ciclo, senal, sizing, rebalance, orden, proteccion, guardian, capital y analytics.
6. **Insights derivados:** `data/history/insights.json` para conclusiones automaticas sobre rendimiento, riesgo, simbolos, direccion, regimen, tiempo y salidas.
7. **Trade Inspector:** reconstruccion de trades individuales desde historia, decisiones, snapshots, timeline y analytics.
8. **Interfaces read-only:** Telegram, dashboard, analyzers, healthcheck.

## Historical Persistence

`trading/history.py` es una capa append-only encapsulada para memoria historica. No participa en scoring, seleccion de monedas, sizing, TP/SL, rebalance ni entradas/salidas.

```text
analytics.py
   |
   +-- record_trade_open()  -> data/history/trades.jsonl
   +-- record_trade_close() -> data/history/trades.jsonl
   +-- record_decision()    -> data/history/decisions.jsonl
   +-- record_snapshot()    -> data/history/snapshots.jsonl

analytics_engine.py
   |
   +-- rebuild_statistics() -> data/history/stats.json
   +-- update_trade()       -> actualiza solo agregados afectados
   +-- get_*_stats()        -> lee solo stats.json

feature_store.py
   |
   +-- record_trade_features() -> append en data/history/features.jsonl

insights_engine.py
   |
   +-- rebuild_insights()   -> data/history/insights.json desde stats.json
   +-- load_insights()      -> reconstruye insights si falta/corrupto
   +-- get_*_insights()     -> lee conclusiones ya generadas

decision_timeline.py
   |
   +-- record_event()       -> data/history/timeline.jsonl
   +-- read_recent_events() -> ultimos eventos, con filtro por categoria/simbolo
   +-- compact_event_for_telegram()

trade_inspector.py
   |
   +-- inspect_trade()      -> reporte completo por trade_id o simbolo/fecha
   +-- inspect_latest()     -> ultimo trade / ultimo ganador / ultimo perdedor
   +-- format_for_telegram()
```

Propiedades:

- JSONL, un objeto por linea.
- Append-only para escritura.
- Crea archivos/directorios si no existen.
- Si encuentra JSON invalido durante lectura, registra WARNING y continua.
- `get_trade(trade_id)` reconstruye el ultimo estado conocido del trade.
- `stats.json` no es fuente de verdad: si falta o esta corrupto, se reconstruye desde JSONL.
- `timeline.jsonl` rota al superar 5 MB y preserva eventos recientes.
- `trade_inspector.py` tolera datos incompletos y lineas corruptas mostrando `Dato no disponible`.
- Los archivos runtime bajo `data/history/` no se versionan.

## Riesgos Arquitectonicos

- `bot.py` sigue concentrando la orquestacion principal, aunque cierres/parciales, auditoria y persistencia ya fueron extraidos.
- `state.json` puede desalinearse del exchange si hay cierres externos, fallos parciales o ejecuciones manuales.
- `binance_client.py` ya centraliza el acceso a Binance, pero todavia falta implementar clientes alternativos como `FakeBinanceClient`, `ReplayBinanceClient`, `PaperBinanceClient` y `ShadowBinanceClient`.
- Los JSONL no tienen una politica formal de retencion.
- Hay documentos legacy dentro de `trading/` que ahora apuntan a docs canonicas.
