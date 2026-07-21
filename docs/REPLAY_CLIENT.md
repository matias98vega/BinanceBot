# ReplayClient offline

`ReplayClient` reproduce una secuencia temporal versionada sobre el mismo
`FakeExchangeState` y el mismo motor de órdenes de `FakeBinanceClient`. Es una
herramienta de tests y análisis: no es un backtester, no selecciona parámetros y
no puede importarse ni activarse desde el runtime productivo.

La biblioteca permanente de incidentes sanitizados, su taxonomía de evidencia y
sus límites están en docs/REPLAY_INCIDENT_FIXTURES.md. Se ejecutan con
run_replay_scenario.py --incident; ninguna fixture actual declara fidelidad total.

## Niveles de fidelidad

- `FIXTURE_REPLAY`: fixture diseñada para tests. No admite `missing_fields` y se
  espera cobertura completa del escenario declarado.
- `RECORDED_OBSERVATION_REPLAY`: observaciones sanitizadas. Reproduce únicamente
  lo registrado y declara faltantes en `missing_fields`.
- `HISTORICAL_EVENT_REPLAY`: convierte historia existente en eventos de análisis.
  No inventa balances, respuestas, klines, posiciones ni fills ausentes.

Que un tape sea completo significa que cubre su fixture declarada, no que emule
todo Binance. No se modelan matching engine, latencia, slippage, funding
programado, liquidaciones ni endpoints fuera del contrato del fake.

## Arquitectura y contrato

- `ReplayTape`: schema inmutable v1, UTC, orden temporal validado y fingerprint
  SHA-256 canónico.
- `ReplayEvent`: evento tipado, timestamp epoch en milisegundos, secuencia estable
  y payload defensivamente copiado.
- `ReplayCursor`: cursor monotónico; no permite retroceder.
- `ReplayClient(FakeBinanceClient)`: aplica el tape sobre el fake, conserva sus
  IDs, `Decimal`, validaciones, órdenes, OCO, Futures, errores y call log.
- `ReplayScenarioRunner`: ejecuta un callback de ciclo por lote temporal, sin
  `sleep` ni reloj real.

No se importa `binance_client.py`, no se leen credenciales y no existe fallback a
red. Un endpoint desconocido conserva el fail-closed `NotImplementedError`.

## Tape schema v1

```json
{
  "replay_schema_version": 1,
  "scenario_id": "spot-long-tp",
  "mode": "FIXTURE_REPLAY",
  "description": "fixture determinística",
  "timezone": "UTC",
  "started_at": "2023-11-14T22:13:20Z",
  "initial_state": {
    "balances": {"USDT": {"free": "100", "locked": "0"}},
    "prices": {"BTCUSDT": "100"},
    "futures_positions": [],
    "open_orders": []
  },
  "missing_fields": [],
  "events": []
}
```

Eventos soportados: `PRICE`, `KLINES`, `BALANCE`, `FUTURES_WALLET`,
`FUTURES_POSITION`, `SPOT_ORDER`, `FUTURES_ORDER`, `OCO_CREATE`, `OCO_TRIGGER`,
`ORDER_SNAPSHOT`, `FILL_SNAPSHOT`, `ERROR`, `RECONCILIATION`, `PAUSE` y
`OPERATIONAL_EVENT`.

Los eventos de acción (`SPOT_ORDER`, `FUTURES_ORDER`, OCO) llaman al motor del
fake. Los snapshots registrados reemplazan sólo los campos explícitos; no
recalculan ni infieren balances faltantes. Errores se encolan contra la operación
exacta y se consumen con la misma semántica del fake.

## CLI

```bash
.venv/bin/python trading/testing/run_replay_scenario.py --list
.venv/bin/python trading/testing/run_replay_scenario.py --scenario spot-long-tp
.venv/bin/python trading/testing/run_replay_scenario.py --scenario spot-long-tp --json
.venv/bin/python trading/testing/run_replay_scenario.py --tape /ruta/tape.json --strict
.venv/bin/python trading/testing/run_replay_scenario.py --incident incident-ada-stale-spot --json
```

La CLI es read-only salvo `--output DIR`, que escribe `replay_result.json` en la
ruta solicitada. `--strict` retorna 2 para tapes parciales. Nunca escribe history,
state, Timeline ni datos productivos.

## Datos históricos disponibles

`trades.jsonl`, `features.jsonl`, `snapshots.jsonl`, `decisions.jsonl`, Timeline y
evidencia operativa permiten reconstrucción analítica parcial. No contienen todas
las respuestas del exchange, book, latencia, balances intermedios, órdenes y fills
necesarios para equivalencia exacta. Por eso `historical_event_tape()` los marca
explícitamente como `HISTORICAL_EVENT_REPLAY` incompleto.

## Uso en tests

Los tests bloquean socket y DNS, usan tapes en memoria o `TemporaryDirectory`, y
comparan ejecuciones repetidas. Un callback puede ejecutar una iteración completa
de consumidores contra el contrato del cliente, pero el ReplayClient no está
conectado automáticamente a `CycleRunner` ni a módulos productivos.
