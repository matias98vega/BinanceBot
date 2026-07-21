# Gap and downtime policy

La ausencia de trades no demuestra downtime. Un intervalo sólo se explica con
evidencia persistida durante ese intervalo; nunca con el estado actual aplicado
retrospectivamente, systemd, journal, commits o memoria humana.

## Estado,  y evidencia

`data/history/operational_state.jsonl` registra transiciones únicamente cuando
cambian estado/razón, un `cycle_completed` compacto por ciclo y un heartbeat cada
900 segundos como máximo. Estados: `RUNNING`, `IDLE_NO_SIGNAL`, `PAUSED_RISK`,
`PAUSED_MANUAL`, `BLOCKED_CAPACITY`, `BLOCKED_RECONCILIATION`,
`BLOCKED_EXCHANGE_UNKNOWN`, `MAINTENANCE`, `DEGRADED`, `STOPPED_UNKNOWN` y
`ERROR`.

El heartbeat cubre 1200 segundos por default. No se extiende indefinidamente. Los
umbrales configurables son `OPERATIONAL_HEARTBEAT_INTERVAL_SECONDS`,
`GAP_HEARTBEAT_COVERAGE_SECONDS`, `GAP_OPERATIONAL_EVIDENCE_MAX_AGE_SECONDS` y `GAP_MIN_DURATION_HOURS`; no se modifican en
`.env` durante esta tarea.

Fuentes válidas: transiciones, heartbeat, cycle completed, pausas, reconciliación,
mantenimiento explícito, gate material, error y Guardian persistidos. Eventos
DEBUG no aportan cobertura operativa.

## Clasificaciones

`EXPLAINED_NO_SIGNAL`, `EXPLAINED_CAPACITY`, `EXPLAINED_RISK_PAUSE`,
`EXPLAINED_MANUAL_PAUSE`, `EXPLAINED_RECONCILIATION`,
`EXPLAINED_MAINTENANCE`, `EXPLAINED_EXCHANGE_DEGRADED`,
`PARTIALLY_EXPLAINED`, `UNEXPLAINED_DOWNTIME`,
`UNKNOWN_INSUFFICIENT_EVIDENCE` y `LEGACY_NO_EVIDENCE`.

La cobertura completa exige al menos 95% del intervalo. La herramienta no hace
backfill ni reclasifica gaps previos al primer registro de evidencia.

## CLI

```bash
.venv/bin/python trading/analyze_operational_gaps.py --explain
.venv/bin/python trading/analyze_operational_gaps.py --json
```

`--strict` retorna 2 si encuentra downtime inexplicado o cobertura parcial. Un
`--output DIR` escribe sólo el reporte solicitado.

Para evidencia manual futura existe `record_operational_event.py`. Requiere acción,
actor y razón explícitos, y sólo agrega evidencia: no pausa procesos ni cambia el
state de trading. En esta tarea no se usa contra producción ni retrospectivamente.

## Limitación histórica

Los gaps anteriores al despliegue permanecen `LEGACY_NO_EVIDENCE` salvo evidencia
real ya persistida que cubra el intervalo. En particular, ONDO→XRP no puede
explicarse usando heartbeats futuros.
