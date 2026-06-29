# BinanceBot Architecture

Este documento es la referencia canonica de arquitectura. Describe el estado actual sin proponer cambios de estrategia.

## Vista General

```text
Binance API
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
   +-- write analytics and decision snapshots
   +-- build/persist bot_state.json
   +-- save state.json
   +-- release lock
```

El ciclo principal vive en `trading/bot.py`. Su responsabilidad es orquestar; las decisiones de mercado viven en `market.py`, la ejecucion Long en `longs.py`, la ejecucion Short en `shorts.py`, los guardrails en `capital_manager.py` y la observabilidad en `analytics.py`/`bot_state.py`.

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

bot._handle_close()
   |
   +-- calcula/recibe PnL
   +-- actualiza daily/total PnL
   +-- registra cooldown si corresponde
   +-- escribe trades_log y analytics
   +-- remueve posicion de state
```

Los cierres parciales se manejan desde `bot.py` en `_check_partial_long` y `_check_partial_short`.

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
| `bot.py` | Orquestar ciclo principal, estado, entradas, cierres y observabilidad | `config`, `utils`, `market`, `longs`, `shorts`, `rebalance`, `capital_manager`, `bot_state`, `analytics` | systemd/manual |
| `capital_manager.py` | Guardrails de capital, max margin por posicion, validacion y snapshot | env, estado | `bot.py`, `longs.py`, `shorts.py`, dashboard |
| `rebalance.py` | Mover USDT entre Spot/Futures segun regimen | `utils`, `config`, `state` | `bot.py`, `bot_state.py` |
| `longs.py` | Apertura y gestion Long Spot | `utils`, `config`, `capital_manager` | `bot.py`, recovery |
| `shorts.py` | Apertura y gestion Short Futures | `utils`, `config`, `capital_manager` | `bot.py` |
| `sl_guardian.py` | Proteccion independiente de SL | `utils`, `config`, `analytics` | systemd/manual |
| `market.py` | Contexto BTC, scoring, filtros y candidatos | `utils`, `config` | `bot.py` |
| `utils.py` | HTTP Binance, firma, balances, filtros, indicadores, state, logs, alerts | `config` | casi todos los modulos |
| `config.py` | Constantes de estrategia/riesgo y runtime config | `config_loader` | modulos de trading |
| `config_loader.py` | Cargar `.env` y rutas sin requerir API en herramientas read-only | `.env` | `config`, dashboard, Telegram, tools |
| `analytics.py` | Eventos JSONL, snapshots, export CSV y puente hacia historia pasiva | runtime config | `bot.py`, guardian, analizadores |
| `history.py` | Memoria historica pasiva de trades, decisiones y snapshots | JSONL en `data/history/` | `analytics.py`, tests, futuras herramientas offline |
| `bot_state.py` | Snapshot observable de estado/capital/sistema | `state`, `capital_manager`, `rebalance`, systemd | `bot.py`, Telegram, dashboard |
| `telegram_commands.py` | Menu y comandos read-only | `bot_state`, JSONL, `state` | servicio Telegram |
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
| `trading/.cycle_baseline.json` | JSON | Baseline local pre/post ciclo |
| `trading/telegram_offset.json` | JSON | Offset de updates Telegram |
| `trading/blacklist_dynamic.json` | JSON | Blacklist dinamica persistida |

## Observabilidad

La observabilidad actual tiene tres capas:

1. **Estado actual:** `state.json` y `bot_state.json`.
2. **Historico estructurado:** `trade_analytics.jsonl` y `decision_snapshots.jsonl`.
3. **Memoria historica pasiva:** `data/history/trades.jsonl`, `data/history/decisions.jsonl`, `data/history/snapshots.jsonl`.
4. **Interfaces read-only:** Telegram, dashboard, analyzers, healthcheck.

No existe aun un `decision_timeline.py` en el estado actual del codigo; queda como item pendiente en backlog/roadmap.

## Historical Persistence

`trading/history.py` es una capa append-only encapsulada para memoria historica. No participa en scoring, seleccion de monedas, sizing, TP/SL, rebalance ni entradas/salidas.

```text
analytics.py
   |
   +-- record_trade_open()  -> data/history/trades.jsonl
   +-- record_trade_close() -> data/history/trades.jsonl
   +-- record_decision()    -> data/history/decisions.jsonl
   +-- record_snapshot()    -> data/history/snapshots.jsonl
```

Propiedades:

- JSONL, un objeto por linea.
- Append-only para escritura.
- Crea archivos/directorios si no existen.
- Si encuentra JSON invalido durante lectura, registra WARNING y continua.
- `get_trade(trade_id)` reconstruye el ultimo estado conocido del trade.
- Los archivos runtime bajo `data/history/` no se versionan.

## Riesgos Arquitectonicos

- `bot.py` es el modulo mas grande y con mayor blast radius.
- `state.json` puede desalinearse del exchange si hay cierres externos, fallos parciales o ejecuciones manuales.
- El sistema todavia no tiene una abstraccion de cliente Binance para pruebas de integracion limpias.
- Los JSONL no tienen una politica formal de retencion.
- Hay documentos legacy dentro de `trading/` que ahora apuntan a docs canonicas.
