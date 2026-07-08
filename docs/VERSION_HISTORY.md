# Version History

Este documento registra capacidades, limitaciones, bugs conocidos y fixes por rango historico del bot. Su objetivo es decidir si datos o trades de un periodo pueden usarse, deben excluirse o necesitan una marca de confiabilidad parcial.

El versionado operativo actual sigue leyendo `VERSION`. La clasificacion historica machine-readable vive en `trading/version_history.py`.

## Estado Actual

- Version runtime actual: `v1.1-observability-hardening`.
- Version legacy declarada en `VERSION`: `v1.0-alpha`.
- Granularidad historica anterior: gruesa. Desde `v1.1-observability-hardening`, los nuevos registros llevan metadata runtime explicita.
- Politica de datos actual: usar datos historicos solo con auditoria previa.
- Reparacion de datos: no automatica. `trading/repair_data_quality.py` existe como scaffold dry-run, sin escritura.

## Rangos

| Version | Desde | Hasta | Politica de datos | Confianza |
|---|---|---|---|---|
| `legacy-pre-history` | Desconocido | 2026-06-01 | excluir o revisar manualmente | Baja |
| `v1.0-alpha` | 2026-06-01 | 2026-07-08 | usable con flags de auditoria | Media |
| `v1.1-observability-hardening` | 2026-07-08 | Actual | trusted si auditor reciente no tiene criticos | Alta |

## Metadata Runtime

Los nuevos registros persistidos por el bot deben incluir:

- `bot_version`
- `strategy_version`
- `data_schema_version`

La fuente runtime es `trading/version_history.py`. Los historicos sin metadata pueden clasificarse por timestamp, pero esa inferencia tiene menor confianza que un `bot_version` explicito.

## v1.0-alpha

### Capacidades

- Operacion modular Long Spot y Short Futures.
- Guardian, rebalance, capital manager, Telegram y dashboard.
- History, Feature Store, Timeline, Analytics, Insights y Trade Inspector.
- Capital Ledger y Capital Accounting.
- Auditor local de calidad de datos.
- Futures Reconciliation para observar exchange vs estado local.

### Bugs Conocidos Dentro Del Rango

- Registros tempranos pueden no tener `bot_version` o `strategy_version`.
- Telegram Home mostro `Shorts: 0/2` aunque el ciclo real decia `Shorts: 0/0`.
- Telegram Estadisticas llego a mostrar PnL Trading falso al interpretar `total_limit` como baseline.
- Futures abiertas en Binance pudieron quedar desincronizadas del estado local antes de Futures Reconciliation.
- Spot residual OCO podia intentar publicar OCO bajo `NOTIONAL`.
- Existe deuda historica conocida alrededor de `short_WLDUSDT_1782763085`.

### Fixes Relevantes

- Reconciliacion Futures para detectar posiciones observadas, no gestionadas, desprotegidas o desincronizadas.
- Validacion de residual Spot usando el payload final exacto de OCO.
- PnL de Home y Estadisticas alineado a `bot_state.pnl`.
- Capital ajustado dejo de usar `total_limit` como capital invertido.
- Rebalance y errores Binance tienen mejor trazabilidad.

### Uso De Datos

- Datos posteriores a cada fix son mas confiables para esa dimension especifica.
- Datos anteriores al fix pueden seguir siendo utiles, pero deben marcarse con limitacion.
- Para ML o backtesting, excluir registros con errores criticos del auditor.
- Para analisis manual, usar `trade_inspector`, `audit_data_quality.py` y `version_history.classify_record(...)`.

## Reparacion Futura

No se debe reescribir historial sin:

- dry-run previo;
- backup automatico;
- checksums antes/despues;
- reporte JSON/Markdown;
- confirmacion explicita;
- reparacion de un solo tipo de problema por ejecucion.

`trading/repair_data_quality.py` actualmente solo genera planes dry-run, incluyendo `--plan version-backfill`, y rechaza `--write`/`--apply`.

## Politica De Confianza

### trusted

- Version con auditor reciente sin errores criticos.
- PnL Home, Estadisticas y logs alineado.
- Sin desync Binance/state.
- Sin posiciones huerfanas no gestionadas.
- Registros con `bot_version`, `strategy_version` y `data_schema_version` explicitos.

### partial

- Version con bugs conocidos de observabilidad.
- Datos utiles para analisis cualitativo, no para ROI final.
- Historicos clasificados por timestamp en vez de `bot_version` explicito.

### excluded

- Registros con cierre sin apertura.
- Posiciones desincronizadas sin reconciliacion.
- JSON/JSONL corrupto sin reparacion auditada.

## Problemas Historicos Conocidos

- `short_WLDUSDT_1782763085`: cierre sin apertura previa; requiere revision manual o migracion auditada.
- Registros antiguos de `features.jsonl` sin `market.regime`.
- Recovery Spot antiguo con campos incompletos.
- Etapa previa a Futures Reconciliation: datos Futures con confianza parcial.
- Etapa previa a PnL unificado Home/Stats: reportes Telegram antiguos pueden no coincidir con PnL real.
- Etapa previa a validacion final de payload OCO: residuales Spot podian generar errores `NOTIONAL` evitables.
- Etapa previa a fix de Shorts compactos: Telegram podia mostrar capacidad incorrecta de shorts.
