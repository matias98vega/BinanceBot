# Roadmap

Este roadmap documenta el estado del proyecto y las lineas de trabajo futuras. No define cambios de estrategia por si mismo; cualquier cambio de trading debe pasar por una iteracion separada, con pruebas y revision de riesgo.

## Estado Actual

BinanceBot es un bot modular de trading para Binance con ejecucion por ciclos, gestion separada de Long Spot y Short Futures, guardian de SL, rebalanceo de capital, guardrails de capital, Telegram, dashboard local y observabilidad JSONL.

El sistema ya cuenta con:

- Ejecucion principal en `trading/bot.py` con lock local.
- Longs Spot en `trading/longs.py` con compra MARKET, OCO, recovery y venta de emergencia.
- Shorts Futures en `trading/shorts.py` con orden MARKET, TP reduceOnly y SL nativo/software.
- Guardian independiente en `trading/sl_guardian.py`.
- Rebalance Spot/Futures en `trading/rebalance.py`.
- Capital guardrails centralizados en `trading/capital_manager.py`.
- Estado observable persistido en `trading/bot_state.py`.
- Telegram read-only en `trading/telegram_commands.py`.
- Alertas Telegram configurables en `trading/telegram_alerts.py`.
- Dashboard local en `dashboard/app.py`.
- Analytics append-only en `trading/analytics.py`.
- Snapshots de decisiones en `trading/decision_snapshots.jsonl`.
- Healthcheck, preflight, post-cycle y validadores de observabilidad.
- Tests unitarios para capital, rebalance, hardening de trades y notificaciones.

## Corto Plazo

| Item | Prioridad | Dependencias | Estado |
|---|---|---|---|
| Documentacion canonica del proyecto | Alta | Ninguna | En curso |
| Consolidar arquitectura en raiz y `docs/` | Alta | Documentacion canonica | En curso |
| Auditar desalineaciones entre docs y codigo | Alta | Inventario de modulos | En curso |
| Completar Decision Timeline cronologico | Alta | Definir contrato JSONL y puntos de integracion | Pendiente |
| Mejorar diagnostico de errores Binance HTTP | Alta | Wrapper HTTP actual | Implementado parcialmente |
| Profundizar tests de recovery Long Spot | Alta | Hardening OCO actual | Implementado base |
| Validar cierre preventivo BTC con orden real de salida | Alta | Auditoria de `bot.py` | Pendiente |
| Separar estado observable de calculos de presentacion | Media | `bot_state.py` actual | En desarrollo |
| Revisar documentos legacy | Media | Docs canonicas | En curso |

## Mediano Plazo

| Item | Prioridad | Dependencias | Estado |
|---|---|---|---|
| Auditoria estado local vs exchange antes de operar | Alta | Helpers seguros de balance/posiciones | Pendiente |
| Backtesting offline del scoring actual | Alta | Dataset historico local | Pendiente |
| Dataset local de klines por simbolo/timeframe | Alta | Politica de almacenamiento | Pendiente |
| Simulador con fees, slippage y filtros Binance | Alta | Dataset historico | Pendiente |
| Reportes por regimen, simbolo, hora y direccion | Media | Analytics estable | Pendiente |
| Dashboard de diagnostico profundo | Media | API local estable | En desarrollo |
| Alertas periodicas de healthcheck | Media | Telegram alerts | Pendiente |
| Rotacion de JSONL operativos | Media | Politica de retencion | Pendiente |
| Pruebas de integracion sin ordenes reales | Media | Mocks Binance consistentes | Pendiente |

## Largo Plazo

| Item | Prioridad | Dependencias | Estado |
|---|---|---|---|
| Motor de experimentacion de estrategias | Alta | Backtesting confiable | Idea |
| Portfolio multi-estrategia | Media | Motor de riesgo compartido | Idea |
| Optimizacion walk-forward | Media | Backtesting y datasets | Idea |
| Adaptacion dinamica de parametros | Media | Estadisticas suficientes | Idea |
| Clasificador offline de regimen de mercado | Baja | Dataset historico | Idea |
| Modelos ML auxiliares para investigacion | Baja | Feature store y evaluacion offline | Idea |
| Comparacion automatica entre versiones | Media | Framework de experimentos | Idea |
| Reportes semanales con hipotesis | Media | Analytics enriquecida | Idea |

## Funcionalidades Implementadas

- Ciclo principal con lock y proteccion contra concurrencia.
- Gestion simultanea de Long Spot y Short Futures.
- Modo direccional por contexto BTC.
- Scoring Long/Short multi-timeframe.
- Cooldown por simbolo tras SL.
- Pausa por racha de SL y perdida diaria.
- Partial take profit.
- Trailing stop.
- Stale exit.
- Guardian SL independiente.
- Rebalance automatico entre Spot y Futures.
- Reserva minima de wallet configurable, default `0`.
- Guardrails de capital por exposicion y slots.
- Telegram read-only con menu, capital, posiciones, health, diagnostico, trades y snapshots.
- Notificaciones Telegram configurables por tipo.
- Dashboard local con estado, trades, snapshots, health y metricas.
- Analytics estructurada y snapshots de decisiones.
- Validadores de observabilidad y healthcheck local.
- Hardening Long Spot para no proteger/vender mas que balance real disponible.

## Funcionalidades En Desarrollo

- Mejoras de UX Telegram.
- Diagnostico de capacidad real por wallet.
- Observabilidad de errores Binance.
- Consolidacion documental.
- Tests de hardening operativo.

## Funcionalidades Pendientes

- Decision Timeline JSONL cronologico de eventos compactos.
- Auditoria state-vs-exchange antes de operar.
- Backtesting offline.
- Dataset historico local.
- Dashboard de analitica avanzada.
- Politica formal de retencion/rotacion de logs.
- Playbooks de recuperacion manual.
- Pruebas de integracion con cliente Binance mockeado.

## Riesgos Conocidos

- `state.json` sigue siendo fuente operativa local; si se desalineara con Binance, pueden aparecer decisiones incorrectas.
- El cierre preventivo BTC debe auditarse para asegurar que siempre ejecuta orden de cierre antes de remover una posicion local.
- En algunos flujos legacy puede haber nombres inconsistentes para OCO (`oco_id` vs `oco_order_list_id`).
- Los JSONL append-only pueden crecer sin rotacion formal.
- Los errores HTTP de Binance dependen de cuerpos y codigos que pueden variar.
- El dashboard y Telegram son read-only, pero dependen de freshness de archivos locales.
- Hay configuracion historica de capital deprecated que conviene eliminar cuando ya no haya compatibilidad pendiente.

## Deuda Tecnica

- Documentacion historica dispersa entre raiz, `trading/` y `docs/`.
- `bot.py` concentra mucha orquestacion, cierre, partials, auditoria de orphans y persistencia.
- Tests aun no cubren todos los flujos de salida y recuperacion.
- No existe cliente Binance abstracto inyectable; los mocks se aplican por patching de funciones.
- Observabilidad de timeline cronologico aun no esta implementada en el codigo actual.
- No hay una politica unica de migracion de `state.json`.
- Algunos comentarios en codigo tienen encoding deteriorado heredado.

## Ideas Futuras

- Feature store offline basado en analytics y snapshots.
- Simulador de ejecucion Binance con filtros reales.
- Ranking de filtros por impacto historico.
- Comparacion live-vs-backtest.
- Panel de riesgo por exposicion, correlacion y drawdown.
- Modo laboratorio separado de modo live.
- Reporte automatico diario/semanal.
