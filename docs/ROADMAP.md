# Roadmap

> Última revisión: 2026-07-21
>
> Commit de referencia: `3134e76`
>
> Versión runtime: `v1.2-sizing-v2`
>
> Estado general: motor live desplegado y observable; contabilidad confiable desde el bootstrap; fase actual centrada en confiabilidad y preparación estadística, sin ML conectado al trading.

Este documento es la hoja de ruta canónica. No autoriza cambios de estrategia ni operaciones reales. Todo cambio live requiere una tarea separada, pruebas, revisión de riesgo y aprobación explícita.

## Estado actual desplegado

| Capacidad | Estado | Evidencia |
|---|---|---|
| Core Long Spot / Short Futures, TP/SL, Guardian y rebalance | COMPLETADO | `trading/longs.py`, `shorts.py`, `sl_guardian.py`, `rebalance.py` |
| Sizing v2 por exposición y slots | COMPLETADO | `VERSION`, `trading/version_history.py`, tests de sizing |
| Analytics Engine y Telegram avanzado | COMPLETADO | `analytics_engine.py`, `telegram_commands.py` |
| Métricas y diagnóstico por versión | COMPLETADO | commits `4566c32`, `d69402f` |
| Diagnóstico exploratorio de SHORT | COMPLETADO | commit `ec4bc57`, `analyze_short_performance.py` |
| Insights Engine base y Trade Inspector | COMPLETADO | `insights_engine.py`, `trade_inspector.py` |
| Capital real/usado/libre y PnL abierto | COMPLETADO | páginas Capital y Posiciones de Telegram |
| Reconciliación de rebalance alineado | COMPLETADO | commit `335da04` |
| Reconciliación segura de Spot stale | COMPLETADO | commit `5fc9fe6` |
| Capital ledger schema v2 y flujos externos | COMPLETADO | commits `5af249f`, `8ded902` |
| Bootstrap productivo del ledger | COMPLETADO | inicio contable `2026-07-20T21:47:14Z` |
| PnL Trading, ROI y uPnL Spot/Futures | COMPLETADO | commit `3134e76`, CLI y Telegram |
| Clasificación del auditor | COMPLETADO | commit `970bd97`; separa crítico, operativo, legacy, informativo y aceptado |
| Capacidad operativa vs target visual | COMPLETADO | `operational_max`, `target_max`, estado no incrementable |

### Convención contable vigente

- `REALIZED_PNL` es PnL realizado neto de comisiones de trading.
- `TRADING_FEE` es informativo y no se vuelve a restar.
- `FUNDING_FEE` conserva signo.
- `trading_pnl_net = realized_pnl_net_of_fees + funding_net`.
- PnL Trading y ROI Trading son confiables sólo desde `2026-07-20T21:47:14Z`; no reconstruyen actividad previa.

## Fase inmediata — sincronización y deuda documental

| Ítem | Estado | Evidencia | Pendiente real | Dependencia | Prioridad |
|---|---|---|---|---|---|
| Sincronizar Roadmap, Changelog y README | COMPLETADO | revisión documental sobre `3134e76` | Mantenerlos por capability update | Ninguna | Alta |
| Inventario de recovered, partials y gaps | PARCIAL | auditor y `VERSION_HISTORY.md` ya los clasifican | Manifiesto único con impacto y política por registro | Auditor actual | Alta |
| Taxonomía Neutral/Sideways | PENDIENTE | analytics conserva ambos buckets; runtime usa principalmente Neutral | Consolidar Sideways visualmente dentro de Neutral sin reescribir históricos | Contrato de presentación | Media |
| Documentar capacidad operativa vs target | COMPLETADO | `bot_state.py`, Telegram Diagnóstico, auditor | Mantener contrato en consumidores futuros | Ninguna | Alta |
| Definir criterios de dataset listo | COMPLETADO | sección específica de este documento | Implementar auditoría que produzca el manifiesto | Feature Store y auditor | Alta |
| Revisar versionado funcional | PARCIAL | runtime y `VERSION` siguen en `v1.2-sizing-v2` | Decidir capability epoch futura sin reetiquetar silenciosamente trades | Criterio de release | Media |
| CHANGELOG por capacidades | COMPLETADO | `CHANGELOG.md` | Actualizar por hito, no por cada commit | Disciplina de release | Media |

## Fase de confiabilidad

| Ítem | Estado | Evidencia | Pendiente real | Dependencia | Prioridad |
|---|---|---|---|---|---|
| Auditoría unificada state-vs-exchange pre-entry | PARCIAL | gate unificado, CLI, Fake E2E e integración AUDIT_ONLY | Observar varios ciclos y autorizar ENFORCE por separado | Evidencia productiva sin falsos positivos | Alta |
| FakeBinanceClient o ReplayClient | COMPLETADO | FakeBinanceClient A-L y ReplayClient offline determinístico sobre el mismo state/contrato | Acumular observaciones sanitizadas; históricos siguen siendo parciales | Tapes versionados | Media |
| Biblioteca de incidentes Replay sanitizados | COMPLETADO | seis fixtures versionadas con fidelidad/confianza explícitas y regresiones offline | Sumar incidentes forward-only sin elevar artificialmente su fidelidad | ReplayClient | Media |
| Presentación Telegram de lectura rápida | COMPLETADO | Home compacta, diagnóstico técnico separado, Neutral/Sideways visual, símbolos paginados y progreso v2 | Mantener contratos read-only | Telegram | Media |
| Tests end-to-end sin operaciones reales | PARCIAL | 473 tests unitarios y supresión de transportes | Escenarios completos ciclo→persistencia→Telegram con cliente falso | Fake/Replay client | Alta |
| Política de gaps/downtime persistida | COMPLETADO | operational_state, heartbeat y CLI reproducible | Acumular evidencia forward-only; legacy no se backfillea | Ciclos futuros | Alta |
| Freshness de stats e insights | PARCIAL | stats se reconstruye; Insights tiene metadata | Umbrales, relación de fuentes y warning visible | Contratos derivados | Media |
| Rotación y retención JSONL | PARCIAL | Timeline rota | Política uniforme, backup y pruebas para los demás JSONL | Inventario de consumidores | Media |
| Timeline operativo vs debug | COMPLETADO | schema v2 y filtros Operational/Diagnostic/Debug | Afinar mappings futuros sin reescribir legacy | Taxonomía canónica | Media |
| Cierre preventivo BTC y fallback Guardian | NECESITA REDEFINICIÓN | riesgo documentado, tests parciales | Validar con cliente falso, replay y GET/read-only; operación real sólo fuera del roadmap y con aprobación explícita | Fake/Replay client | Alta |
| Playbooks de recuperación manual | PENDIENTE | acciones distribuidas en docs y CLI | Procedimientos idempotentes y verificables | Reconciliación unificada | Media |

## Fase de evaluación estadística

| Ítem | Estado | Evidencia | Pendiente real | Dependencia | Prioridad |
|---|---|---|---|---|---|
| Auditoría formal del dataset | COMPLETADO | `audit_ml_dataset.py`, schema 1: 198 TRUSTED, 35 PARTIAL, 2 EXCLUDED | Repetir por snapshot/capability epoch | Criterios de dataset listo | Alta |
| Manifiesto trusted/partial/excluded | COMPLETADO | manifiesto reproducible por `trade_id`, sólo escrito con `--output`/`--manifest` | Consumir únicamente TRUSTED en el baseline | Auditoría formal | Alta |
| Baseline reproducible del scoring actual | COMPLETADO | auditoría: `dataset_ready_for_baseline=true`, 198 cierres TRUSTED | Definir target, periodo, métricas y evaluación temporal agrupada | Dataset trusted | Alta |
| Intervalos de confianza | PARCIAL | diagnóstico SHORT incluye bootstrap CI | Generalizar por versión, lado y régimen | Muestras válidas | Alta |
| Mínimos de muestra | PARCIAL | Insights y SHORT usan umbrales | Política común por reporte y decisión | Baseline | Alta |
| Comparación robusta entre versiones | PARCIAL | métricas y diagnóstico por versión ya existen | Comparación pareada/temporal, intervalos y control de mix | Manifiesto y baseline | Alta |
| Detección de drift | PENDIENTE | no existe monitor estadístico formal | Drift de features, labels, símbolos y performance | Dataset versionado | Media |
| Feature schema pre-entry v2 | COMPLETADO | registro canónico, captura local y auditor semántico | Acumular al menos 150 cierres nuevos sin backfill | Feature Store | Alta |
| Walk-forward básico sin ML | PENDIENTE | SHORT sólo informa muestra insuficiente | Framework temporal general contra baseline | Baseline reproducible | Alta |

## Fase ML offline

| Ítem | Estado | Evidencia | Pendiente real | Dependencia | Prioridad |
|---|---|---|---|---|---|
| Export tabular versionado | PENDIENTE | auditoría produce manifiesto/fingerprint, pero no dataset de entrenamiento | Exportar sólo TRUSTED con schema, checksum y lineage | Baseline aprobado | Alta |
| Split temporal reproducible | PENDIENTE | no existe pipeline ML | Train/validation/test sin mezcla temporal | Export tabular | Alta |
| XGBoost offline | COMPLETADO | experimento CPU reproducible, sin conexión live | Repetir con más datos y mejores features estables | Baseline y split temporal | Media |
| Comparación contra baseline | BLOQUEADO | baseline pendiente | Métricas predictivas, económicas y de riesgo | XGBoost offline | Alta |
| Análisis de leakage | PENDIENTE | features históricas mezclan fuentes y tiempos | Verificar disponibilidad estrictamente pre-entry | Auditoría formal | Alta |
| Importancia estable de features | BLOQUEADO | requiere folds temporales | Estabilidad, SHAP/importance y sensibilidad | Walk-forward ML | Media |
| Sensibilidad régimen/lado/símbolo | BLOQUEADO | requiere muestra y modelo | Reporte estratificado con intervalos | Modelo validado | Media |

Durante esta fase XGBoost es exclusivamente una herramienta offline y no participa en scoring, sizing, TP/SL ni órdenes.

## Fase shadow mode

| Ítem | Estado | Evidencia | Pendiente real | Dependencia | Prioridad |
|---|---|---|---|---|---|
| Predictor read-only | BLOQUEADO | sólo está descrito en visión futura | Servicio/módulo sin capacidad de ordenar | Modelo offline validado | Alta |
| Persistir predicción hipotética | BLOQUEADO | contrato aún no definido | `model_version`, `probability`, `expected_return`, `confidence`, decisión hipotética y timestamp | Predictor read-only | Alta |
| Comparar con ejecución real | BLOQUEADO | falta shadow dataset | Joins por trade/candidato y resultados posteriores | Persistencia shadow | Alta |
| Calibración y cobertura | BLOQUEADO | falta muestra shadow | Curvas de calibración, abstención y cobertura | Muestra mínima | Alta |
| Cero impacto live | PENDIENTE | principio documentado | Tests que impidan importar rutas de ejecución/mutación | Diseño shadow | Crítica |

## Fase de posible activación conservadora

| Ítem | Estado | Evidencia | Pendiente real | Dependencia | Prioridad |
|---|---|---|---|---|---|
| Superar baseline y walk-forward | BLOQUEADO | no existen resultados todavía | Umbrales predefinidos y repetibles | Fases anteriores | Crítica |
| Veto conservador inicial | BLOQUEADO | no autorizado | Diseño como filtro de abstención, nunca generador autónomo inicial | Shadow exitoso | Alta |
| Feature flag y rollback inmediato | BLOQUEADO | no aplica aún | Contrato, default off y rollback probado | Diseño de activación | Crítica |
| Límites estrictos y evaluación por versión | BLOQUEADO | no aplica aún | Guardrails y métricas por capability epoch | Activación aprobada | Crítica |
| Sizing adaptativo | BLOQUEADO | explícitamente fuera de alcance | Requiere evidencia posterior independiente | Activación conservadora estable | Muy baja |

## Criterios para considerar el dataset listo

El dataset sólo puede pasar a evaluación estadística/ML cuando exista evidencia reproducible de:

- relación apertura/cierre consistente y labels confiables;
- ausencia de duplicados críticos;
- feature snapshot capturado antes de la entrada;
- exclusión o tratamiento explícito de recovered y partials no confiables;
- cobertura mínima definida por versión, lado y régimen;
- cobertura por símbolo suficiente o agrupación justificada;
- missingness por feature bajo un umbral declarado;
- timestamps válidos, ordenables y con semántica conocida;
- ausencia de features posteriores al resultado o cualquier otro leakage;
- split temporal reproducible;
- manifiesto por fila `trusted`, `partial` o `excluded`, con motivo;
- dataset, schema, reglas de selección y checksums versionados;
- generación determinista desde fuentes inmutables o respaldadas.

Cumplir estos criterios no autoriza cambios live: sólo habilita evaluación offline.

## Criterios de activación ML

- XGBoost no modifica trading durante la fase offline.
- Shadow mode read-only es obligatorio antes de cualquier activación.
- La muestra mínima debe definirse antes de observar resultados shadow.
- El modelo debe superar un baseline reproducible, no sólo obtener PnL positivo.
- La mejora debe ser estable en walk-forward y fuera de muestra.
- La calibración y cobertura deben cumplir umbrales predefinidos.
- No puede existir degradación material de drawdown u otras métricas de riesgo.
- Toda integración debe tener feature flag default-off y rollback inmediato probado.
- La primera activación posible sería un veto conservador; no generación autónoma de entradas.
- La evaluación debe quedar separada por versión/capability epoch.
- El sizing adaptativo permanece bloqueado y requiere una fase futura independiente.

## Deuda técnica preservada

- Registros históricos sin `bot_version` explícito.
- `short_WLDUSDT_1782763085` tiene apertura reparada de forma auditable, pero permanece `PARTIAL` por origen recovered y falta de snapshot completo.
- Recovered opens con schema reducido.
- Partials cuya relación con el trade base necesita normalización cuidadosa.
- Gaps recientes sin evidencia persistida suficiente.
- JSONL sin política uniforme de rotación.
- Nombres legacy de OCO (`oco_id` / `oco_order_list_id`).
- Paper/Shadow clients siguen pendientes; Fake y Replay offline ya son reutilizables.
- Encoding deteriorado en algunos comentarios históricos.
- Versionado operativo demasiado grueso para algunos fixes por capacidad.

## Riesgos y reglas permanentes

- `state.json` sigue siendo fuente operativa; la reconciliación debe ser conservadora y nunca fabricar PnL.
- Un warning operativo real no se reclasifica como aceptado sin evidencia reproducible.
- Los históricos no se reescriben sin dry-run, backup, checksums, confirmación explícita y tests.
- Ledger y contabilidad no infieren depósitos/retiros desde variaciones de equity.
- La versión de un trade es la versión con la que fue abierto.
- Neutral/Sideways se consolidará sólo en presentación; no se cambiarán registros históricos.
- Ningún componente offline, Insights o ML puede enviar órdenes o alterar decisiones live.

## Próximas cinco tareas priorizadas

1. Baseline estadístico reproducible sobre los 198 cierres `TRUSTED`.
2. Evaluación temporal agrupada y version-aware para resolver dependencia/version mixing.
3. Ampliar tapes sanitizados y escenarios ciclo→persistencia→Telegram sin operaciones.
4. Observar el gate pre-entry en AUDIT_ONLY y preparar decisión separada de ENFORCE.
5. Política persistida de gaps/downtime y separación Timeline operativo/debug.

## Documentos relacionados

- `../ARCHITECTURE.md`: arquitectura canónica.
- `BACKLOG.md`: inventario detallado de tareas.
- `CHANGELOG.md`: hitos por capacidad.
- `VERSION_HISTORY.md`: confiabilidad por versión y uso de datos.
- `FUTURE_VISION.md`: visión de largo plazo, subordinada a este roadmap.
