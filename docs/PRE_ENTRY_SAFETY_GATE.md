# 2026-07-25 corrective incident note

The AMDUSDT orphan/unprotected residual was closed by an authorized human operator and recorded separately as `RESIDUAL_CLEANUP_CORRECTIVE_CLOSE`. Gate evaluations were not rewritten; normal forward-only observations determine subsequent state.

# Pre-entry state/exchange safety gate

Versionado: `AUDIT_ONLY` es la capability no conductual `preentry-audit-v1`. Pasar a `ENFORCE` puede bloquear entradas y requiere otra `bot_version` aprobada; ver `VERSIONING_POLICY.md`. Ese cambio no está activo.

## Propósito y alcance

El gate compara el estado gestionado local con una observación read-only del
exchange inmediatamente antes de una apertura. No decide señales, sizing, TP/SL
ni capacidad; sólo reutiliza esas decisiones y evita exposición nueva cuando la
evidencia material es insegura o desconocida.

No cierra posiciones, cancela órdenes, transfiere fondos, repara estado ni escribe
analytics de trades. `BinanceClient` y los payloads productivos no cambian.

## Modos

- `AUDIT_ONLY` (default): evalúa y registra qué habría bloqueado, pero permite que
  la política existente continúe. Es el modo recomendado inicialmente.
- `ENFORCE`: un resultado inseguro se rechaza antes de leverage/BUY/SELL MARKET.

La variable `PRE_ENTRY_GATE_MODE` sólo reconoce literalmente `ENFORCE`; cualquier
otro valor conserva `AUDIT_ONLY`. Esta tarea no modifica `.env` ni systemd y no
activa enforcement productivo.

## Checks canónicos

`LOCAL_STATE_VALID`, `EXCHANGE_READ_COMPLETE`,
`MANAGED_POSITIONS_MATCH_OBSERVED`, `NO_ORPHAN_POSITIONS`,
`EXISTING_POSITIONS_PROTECTED`, `NO_PENDING_RECONCILIATION`,
`CAPACITY_AVAILABLE`, `NO_DUPLICATE_SYMBOL`, `BALANCES_RELIABLE`,
`NO_ACTIVE_RISK_PAUSE`, `NO_UNKNOWN_ORDER_STATE` y
`SIDE_SPECIFIC_STATE_CONSISTENT`.

Cada check expone `passed`, `blocking`, `status_code`, `severity`, `reason`,
`evidence`, `source`, `observed_at`, `stale` y `unknown`. El resultado conserva
todos los motivos aunque seleccione un estado principal.

## Status y prioridad

1. `BLOCKED_ACTIVE_RISK_STATE`
2. `BLOCKED_EXCHANGE_STATE_UNKNOWN`
3. `BLOCKED_ORPHAN_POSITION`
4. `BLOCKED_POSITION_MISMATCH`
5. `BLOCKED_MISSING_PROTECTION`
6. `BLOCKED_RECONCILIATION_PENDING`
7. `BLOCKED_DUPLICATE_SYMBOL`
8. `BLOCKED_CAPACITY`
9. `BLOCKED_BALANCE_UNRELIABLE`
10. `BLOCKED_LOCAL_STATE_INVALID`
11. `SAFE_TO_ENTER`

## Freshness y tolerancias

- Observación exchange: 180 segundos por default.
- Cantidad: `1e-6`, absoluta o relativa a la posición.
- Protección: `1e-6`.

Se pueden ajustar con `PRE_ENTRY_EXCHANGE_OBSERVATION_MAX_AGE_SECONDS`,
`PRE_ENTRY_POSITION_QTY_TOLERANCE` y `PRE_ENTRY_PROTECTION_TOLERANCE`. No se
modifican valores desplegados. Observaciones provistas por el ciclo se reutilizan
sin nuevas llamadas. Una lectura live usa sólo account, positions y open orders
GET ya existentes.

## Spot, Futures y protección

Spot compara la cantidad local con `free + locked`. Una protección válida requiere
dos legs activas del mismo OCO, una limit y una stop, con cantidad compatible. Dust
sin posición gestionada no bloquea otro símbolo.

Futures exige que cada short local corresponda a `positionAmt < 0` y que la
cantidad sea compatible. Una posición sin state es orphan. La protección requiere
órdenes activas `reduceOnly`, lado BUY y cobertura suficiente. El gate no cambia
el fallback ni la política del Guardian.

## Integración

`CycleRunner` evalúa y registra el gate luego de seleccionar el candidato e
inmediatamente antes de `open_long`/`open_short`. Ambos módulos verifican otra vez
el resultado justo antes del primer efecto exchange. Si en el futuro `ENFORCE`
está activo y un caller omite el resultado, realizan una evaluación defensiva.

El resumen queda en `_last_pre_entry_gate` del state y se proyecta de forma
compacta en BotState. Timeline recibe un evento de decisión; no se crea trade ni
feature snapshot de apertura por el gate.

## CLI read-only

```bash
.venv/bin/python trading/check_pre_entry_safety.py --side LONG --symbol BTCUSDT
.venv/bin/python trading/check_pre_entry_safety.py --json
.venv/bin/python trading/check_pre_entry_safety.py --explain
```

`--strict` retorna 2 cuando el estado lógico es inseguro, incluso en AUDIT_ONLY.
`--offline-fixture PATH` consume una observación JSON y evita toda red.

## FakeBinanceClient y pruebas

Los tests cubren aperturas seguras LONG/SHORT, mismatch, cierre externo, dust,
OCO ausente, orphan Futures, protección reduceOnly ausente, timeout, stale,
capacidad, circuit breaker, reconciliación, rebalance benigno, duplicados,
múltiples bloqueos, prioridad, idempotencia y no-network. Los snapshots del fake
demuestran ausencia de mutaciones ante bloqueo.

## Evidencia operativa

Los resultados futuros del gate se clasifican en Timeline v2: bloqueos materiales como OPERATIONAL, capacidad y audit-only como DIAGNOSTIC. Esto no cambia status, decisión ni modo AUDIT_ONLY.

## Activación recomendada

Mantener `AUDIT_ONLY` durante varios ciclos, revisar timeline y BotState y repetir
la CLI sobre ambos lados. Activar `ENFORCE` únicamente en una tarea separada tras
descartar falsos positivos de cantidades Spot compartidas, OCO y órdenes Futures.

## Evidencia durable shadow

La capability no conductual `preentry-evidence-v1` persiste evaluaciones futuras en `data/history/pre_entry_gate_evidence.jsonl`. No existe backfill. Cada `GATE_EVALUATION` conserva identidad determinística, modo y decisión original, freshness, contexto de capacidad/reconciliación y mismatches sanitizados con cantidades Decimal, precio observado durante la gestión, filtros ya cacheados y una matriz shadow compacta. `GATE_ENTRY_OUTCOME` correlaciona por `cycle_id` la apertura o no apertura sin reescribir la evaluación.

La captura es append-only con `O_APPEND`, lock interproceso, una línea JSON UTF-8 y deduplicación best-effort por tail scan. Un fallo se registra y nunca cambia `safe_to_enter`, `entry_allowed`, reason codes ni ejecución. No se agregan llamadas de red: filtros y precios ausentes se guardan como `null`, y las políticas shadow fallan cerradas.

Políticas observacionales: `CURRENT`, `STEP_ONLY`, `RELATIVE_ONLY`, `NOTIONAL_ONLY` y `COMBINED_CONSERVATIVE`. La combinada usa solamente para análisis `absolute_floor=1e-6`, `step_multiplier=1`, `relative_limit=0.001` y `notional_limit_usdt=0.10`. Ninguna política es importada por `pre_entry_safety_gate.py` ni participa en decisiones.

```bash
.venv/bin/python trading/analyze_pre_entry_gate_evidence.py --explain
.venv/bin/python trading/analyze_pre_entry_gate_evidence.py --policy ALL --json
```

`enforce_relevant_evaluation` exige candidato en pre-entry, evaluación completa y capacidad/reconciliación aptas. `false_block_rate` divide falsos positivos shadow entre evaluaciones bloqueantes enforce-relevant; no incluye ciclos sin candidato, gestión, Telegram ni heartbeats. Con menos de 30 evaluaciones se informa muestra baja.

### Shadow step/dust v2

`preentry-tolerance-shadow-v2` es una evaluación aditiva, pura y observacional. Se persiste dentro de nuevos `GATE_EVALUATION` sin elevar el schema general 1, no reescribe registros previos y el analyzer reproduce filas v1 que todavía no tienen esa sección. Las políticas shadow v1 continúan presentes.

V2 clasifica `SAFE_NON_OPERABLE_DUST` solamente para un mismatch de posición LONG gestionada y protegida, con observación completa/fresca, sin fallback, orphan, unknown order ni riesgo de reconciliación, cantidades signed finitas y mismo signo, filtros positivos y precio auditable. Con Decimal exige simultáneamente:

```text
difference = abs(exchange_quantity - local_quantity)
conservative_price = max(precios positivos y frescos persistidos)
difference_notional = max(notional persistido válido, difference * conservative_price)
difference < step_size
difference < min_qty
difference_notional < minimum_notional
difference_notional <= 0.50 USDT
```

Igualar un step o `minQty` bloquea. Un mismatch de posición SHORT usa `BLOCK_UNVALIDATED_SIDE`; esto no impide que un candidato SHORT coexista con dust LONG seguro. Datos faltantes, stale, ambiguos o inválidos fallan cerrados. Los reasons tienen orden estable y cubren evidencia incompleta, filtros/precio, side, protección, reconciliación, operabilidad, step, cap y acumulación observacional. Si hay varios mismatches, todos deben ser seguros.

`GATE_ENTRY_OUTCOME` futuro conserva decisión CURRENT, decisión v2 y `outcome_reason=null` cuando el pipeline no conoce la causa; no inventa una causa ni modifica la apertura. La metadata de acumulación es nullable y nunca suma repetidamente el mismo residual.

La captura v2 puede retirarse sin tocar CURRENT. `pre_entry_safety_gate.py` no importa v2 y `safe_to_enter`, `entry_allowed`, tolerancias y modo continúan sin cambios. Revisar shadow v2 durante al menos 28 días y 300 casos completos, con balance LONG/SHORT y casos de frontera. ENFORCE continúa bloqueado y requiere evaluación, versión conductual y rollback separados.
