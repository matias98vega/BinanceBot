# 2026-07-21 — ReplayClient offline determinístico

- Tapes schema v1 inmutables, fingerprint canónico y cursor monotónico.
- Replay fixture, observación grabada e historia parcial con faltantes explícitos.
- Reutilización de FakeExchangeState para precios, balances, posiciones, órdenes, fills, OCO, errores, pausas, reconciliación y eventos operativos.
- Runner/CLI sin red, sin fallback productivo y sin integración live.

# 2026-07-21 — Passive pre-entry feature context v2

- Added forward-only feature schema/capture versioning.
- Added local pre-entry trend, momentum, volatility, volume, structure, BTC, risk and position-context features without new exchange calls.
- Added the read-only semantic audit, readiness gate and benchmark.
- Strategy, scoring, sizing, order payloads, bot version and historical records remain unchanged.

# 2026-07-21 — Pre-entry state/exchange safety gate

- Added a unified read-only safety evaluation with stable status codes and composable checks.
- Integrated AUDIT_ONLY before LONG/SHORT openings; ENFORCE remains inactive pending production observation.
- Added FakeBinanceClient end-to-end coverage, read-only CLI and compact BotState/timeline evidence.
- Strategy, scoring, sizing, TP/SL, Guardian, reconciliation, payloads and bot version remain unchanged.

# 2026-07-21 — Persistent operational gap evidence

- Added canonical operational states, transition evidence, spaced heartbeats and compact cycle completion.
- Added reproducible persisted-evidence-only gap analysis and explicit maintenance evidence CLI.
- Split Timeline views into Operational, Diagnostic and Debug without rewriting history.
- Trading logic, gate mode, Binance payloads and runtime version remain unchanged.

# Changelog

Resumen de capacidades desplegadas. El historial Git conserva el detalle de cada cambio; este documento registra hitos de alto nivel sin inventar versiones runtime ni fechas no formalizadas.

## v1.0 — Core Trading Engine

- Motor modular con ciclos y lock local.
- Long Spot y Short Futures.
- TP/SL, OCO, partial take profit, trailing y stale exit.
- Guardian de SL independiente.
- Rebalance inicial Spot/Futures.
- Cooldowns, pausa por racha de SL y guardrails de capital.
- Telegram y dashboard read-only iniciales.

## v1.1 — Observability Hardening

- Timeline cronológico y snapshots de decisiones.
- Memoria histórica JSONL y Feature Store pasivo.
- Analytics Engine, Insights Engine base y Trade Inspector.
- Auditor de calidad con agrupación por versión.
- Metadata `bot_version`, `strategy_version` y `data_schema_version` en nuevos registros.
- Telegram avanzado con estadísticas, timeline, insights y diagnóstico.
- Reconciliación de posiciones Futures observadas frente al estado local.

## v1.2 — Sizing v2

- Runtime vigente: `v1.2-sizing-v2`.
- Exposición Spot distribuida por slots Long disponibles.
- Exposición Futures definida como notional y margen derivado por leverage.
- Persistencia de versión de apertura en nuevos trades.
- Métricas históricas por versión de apertura y vista paginada en Telegram (`4566c32`).

## v1.2.x — Reliability & Accounting capability updates

Estos cambios son capabilities posteriores dentro de la versión runtime `v1.2-sizing-v2`; no constituyen nuevas etiquetas runtime.

### Diagnóstico y calidad

- Diagnóstico reproducible de rendimiento por versión (`d69402f`).
- Diagnóstico exploratorio específico de SHORT, sin cambios de estrategia (`ec4bc57`).
- Clasificación del auditor en errores críticos, warnings operativos, legacy, informativos y aceptados (`970bd97`).
- Los gaps sin evidencia reproducible permanecen operativos.

### Reconciliación

- Reconciliación automática e idempotente de rebalance pendiente cuando capital observado y targets ya están alineados (`335da04`).
- Reconciliación conservadora de posiciones Spot stale, sin crear cierres ni PnL (`5fc9fe6`).
- Distinción observable entre capacidad operativa, capacidad objetivo y exceso no incrementable (`3134e76`).

### Capital ledger y contabilidad

- Flujos externos de depósito/retiro separados del rendimiento (`5af249f`).
- Capital ledger schema v2, convención contable explícita y bootstrap seguro/idempotente (`8ded902`).
- Bootstrap productivo aplicado; contabilidad confiable desde `2026-07-20T21:47:14Z`.
- `REALIZED_PNL` neto de trading fees, `TRADING_FEE` informativo y `FUNDING_FEE` firmado.
- PnL Trading y ROI Trading calculados desde el inicio contable, sin reconstruir actividad previa.
- Observación read-only de uPnL Spot y Futures, con desglose por posición y fallos explícitos ante datos incompletos (`3134e76`).

### Telegram

- PnL unificado entre Home y Estadísticas.
- Capital real/usado/libre por wallet.
- PnL abierto total.
- Métricas contables, PnL Trading, ROI y uPnL Spot/Futures.
- Métricas y diagnósticos por versión y SHORT.
- Presentación de capacidad operativa frente a target visual.

## Convención de mantenimiento

- `VERSION` y `trading/version_history.py` son la fuente runtime; no se cambian por cada fix sin una decisión explícita de capability epoch.
- Este changelog se actualiza por capacidad desplegada, no por cada commit.
- Los cambios futuros de estrategia o ML deben registrarse como una fase/versionado separado antes de afectar live.
