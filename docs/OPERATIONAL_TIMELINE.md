# Operational Timeline

El Timeline conserva un único JSONL canónico (`data/history/timeline.jsonl`) y
ofrece tres vistas sin duplicar eventos:

- `OPERATIONAL`: ciclos, órdenes/protección, Guardian crítico, pausas y fallos.
- `DIAGNOSTIC`: capacidad, rechazos, rebalance y resultados audit-only.
- `DEBUG`: señales evaluadas, cálculos y detalle técnico repetitivo.

`category` mantiene el dominio funcional legacy (`SYSTEM`, `ORDER`, `RISK`, etc.).
`event_category` controla la vista y no se mezcla con `severity`. Los registros
nuevos usan schema v2 con `event_type`, `event_category`, `severity`,
`occurred_at`, `recorded_at`, `source`, `cycle_id`, `correlation_id`, `symbol`,
`side`, `trade_id`, `operational_state`, `reason_code`, `summary` y `details`.

Los registros viejos no se reescriben: los lectores infieren una categoría y
marcan `legacy_classification=true`.

## Telegram

`/timeline` muestra `OPERATIONAL` por defecto. Los botones permiten alternar a
Diagnóstico o Debug. La vista compacta evita payloads y mantiene hora, categoría,
símbolo/lado y resumen.

## Retención

Se conserva la política existente: al superar 5 MiB se mantienen aproximadamente
4 MiB recientes desde un límite de línea válido. Esta tarea no rota ni trunca el
archivo productivo; la rotación ocurre sólo mediante el comportamiento futuro ya
existente. Líneas corruptas se ignoran al leer y se reportan en auditoría.
