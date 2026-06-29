# Changelog

Resumen de hitos importantes. No intenta reemplazar el historial de commits.

## Hitos Principales

### Modularizacion del bot

- Separacion en `bot.py`, `market.py`, `longs.py`, `shorts.py`, `sl_guardian.py`, `rebalance.py`, `utils.py` y `config.py`.
- Estado operativo unificado en `state.json`.
- Soporte para posiciones Long Spot y Short Futures simultaneas.

### Guardian SL

- Proceso independiente para revisar SLs con mayor frecuencia que el ciclo principal.
- Proteccion de posiciones sin depender solo del timer del bot.
- Manejo de cierres y actualizacion de estado/analytics.

### Long Spot con OCO y Recovery

- Apertura Long con BUY MARKET y OCO SELL TP/SL.
- Recovery automatico para OCO faltante.
- Venta de emergencia si OCO inicial falla.
- Hardening posterior para usar balance real disponible y no cantidad teorica.

### Short Futures

- Apertura Short con SELL MARKET.
- TP reduceOnly.
- SL nativo cuando esta habilitado y fallback/guardian software.
- Trailing y stale exit.

### Partial Take Profit y Trailing

- Cierre parcial al alcanzar parte del recorrido hacia TP.
- Movimiento de SL a breakeven cuando aplica.
- Trailing stop por movimiento favorable.

### Cooldown y Protecciones

- Cooldown por simbolo tras SL.
- Pausa por racha de SL.
- Limite de perdida diaria.
- Filtros de momentum BTC y modo direccional.

### Rebalance Automatico

- Redistribucion Spot/Futures segun regimen BTC.
- Reserva minima configurable por `REBALANCE_MIN_WALLET_USDT`.
- Default actual de reserva: `0`, para permitir mover 100% al wallet objetivo.

### Capital Manager

- Unificacion de sizing/guardrails con `max_margin_per_position`.
- Base principal: `BOT_MAX_EXPOSURE_PERCENT / max_positions`.
- `BOT_MAX_POSITION_PERCENT` queda deprecated.

### Observabilidad

- `trade_analytics.jsonl` para eventos estructurados de trades.
- `decision_snapshots.jsonl` para decisiones aceptadas/rechazadas/skipped.
- `analyze_trades.py`, `analyze_decisions.py`, `validate_observability.py`.
- `healthcheck.py`, `preflight_check.py` y `post_cycle_check.py`.

### Telegram

- Servicio read-only con `/menu`, `/status`, `/capital`, `/positions`, `/health`, `/diagnostics`, `/lasttrades`, `/snapshots`.
- Botonera inline.
- Estado de bot basado en timer systemd cuando corresponde.
- Notificaciones configurables por tipo.

### Dashboard

- Dashboard local read-only.
- APIs para status, trades, snapshots, health y metricas.
- Lectura preferente de `bot_state.json`.

### Deployment

- Scripts operativos en `scripts/`.
- Unidades systemd de ejemplo para bot, guardian, dashboard y Telegram.
- Guia de despliegue Ubuntu 24.04.
