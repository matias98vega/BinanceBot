# Backlog

Este backlog agrupa trabajo pendiente. No implica autorizacion para modificar estrategia ni comportamiento live.

## Bugs

| Item | Prioridad | Complejidad | Impacto esperado |
|---|---|---:|---|
| Auditar cierre preventivo BTC para confirmar orden real antes de remover posicion | Alta | Media | Evitar que el estado local marque cerrada una posicion que sigue abierta |
| Revisar usos legacy de `oco_id` vs `oco_order_list_id` | Alta | Baja | Reducir fallos de cancelacion/proteccion Long Spot |
| Reconciliar `state.json` contra exchange antes de operar | Alta | Alta | Detectar posiciones huerfanas o cierres externos |
| Revisar comportamiento con balances dust no vendibles | Media | Media | Evitar ruido de recovery pendiente imposible de vender |

## Hardening

| Item | Prioridad | Complejidad | Impacto esperado |
|---|---|---:|---|
| Decision Timeline JSONL rotado | Alta | Media | Entender ciclos y eventos sin journalctl |
| Cliente Binance mockeable/injectable | Alta | Alta | Tests mas confiables sin patching disperso |
| Polling de balance post-fill para Long Spot | Media | Media | Reducir carreras tras compra Spot |
| Auditoria de permisos API en setup | Media | Baja | Detectar claves sin permisos adecuados |
| Playbooks de recuperacion manual | Media | Baja | Reducir improvisacion ante fallos |

## UI

| Item | Prioridad | Complejidad | Impacto esperado |
|---|---|---:|---|
| Vista compacta de timeline en dashboard | Alta | Media | Diagnostico rapido |
| Vista de capacidad real por wallet | Alta | Baja | Entender slots disponibles |
| Separar paneles de capital, riesgo y sistema | Media | Media | Lectura mas clara |
| Filtros por simbolo/direccion/regimen | Media | Media | Analisis mas util |

## Telegram

| Item | Prioridad | Complejidad | Impacto esperado |
|---|---|---:|---|
| Pagina `/timeline` | Alta | Media | Ver eventos recientes desde celular |
| Notificaciones de recovery/critical mas claras | Alta | Baja | Mejor respuesta operativa |
| Healthcheck resumido periodico configurable | Media | Media | Detectar degradacion sin revisar manualmente |
| Comandos de diagnostico no mutantes | Media | Media | Investigar sin tocar estado ni ordenes |

## Dashboard

| Item | Prioridad | Complejidad | Impacto esperado |
|---|---|---:|---|
| `/api/timeline` | Alta | Baja | Exponer eventos cronologicos |
| Graficos PnL por dia/simbolo/direccion | Media | Media | Mejor analisis de performance |
| Tabla de rechazos frecuentes | Media | Media | Detectar filtros dominantes |
| Panel de freshness de archivos | Media | Baja | Identificar observabilidad stale |

## Estadisticas

| Item | Prioridad | Complejidad | Impacto esperado |
|---|---|---:|---|
| Win rate y profit factor por regimen | Alta | Media | Medir si el contexto BTC agrega valor |
| Drawdown diario/semanal | Alta | Baja | Medir riesgo acumulado |
| Distribucion de tiempo en posicion | Media | Baja | Evaluar stale exits |
| Analisis de SL/TP distance vs resultado | Media | Media | Entender sensibilidad de salidas |

## Machine Learning Futuro

| Item | Prioridad | Complejidad | Impacto esperado |
|---|---|---:|---|
| Dataset tabular desde analytics/snapshots | Media | Media | Base para investigacion offline |
| Modelo offline de probabilidad TP/SL | Baja | Alta | Evaluar senal predictiva |
| Feature importance de indicadores | Media | Media | Priorizar filtros utiles |
| Deteccion de outliers de simbolo | Media | Media | Alertar condiciones anormales |

## Adaptacion Dinamica

| Item | Prioridad | Complejidad | Impacto esperado |
|---|---|---:|---|
| Ajustes por regimen solo en simulacion | Media | Alta | Evitar cambios live sin evidencia |
| Cooldown dinamico por simbolo | Baja | Media | Ajustar proteccion a comportamiento historico |
| Risk budget por drawdown reciente | Media | Alta | Gestion de riesgo mas profesional |

## Testing

| Item | Prioridad | Complejidad | Impacto esperado |
|---|---|---:|---|
| Tests de cierre preventivo BTC | Alta | Media | Cubrir flujo de riesgo critico |
| Tests de state-vs-exchange reconciliation | Alta | Alta | Evitar operar sobre estado falso |
| Tests de Telegram pages | Media | Media | Evitar regresiones UX |
| Tests dashboard APIs | Media | Baja | Mantener contrato read-only |
| Tests de rotacion JSONL | Media | Baja | Evitar crecimiento sin control |

## Performance

| Item | Prioridad | Complejidad | Impacto esperado |
|---|---|---:|---|
| Cache local de klines por ciclo | Media | Media | Menos llamadas Binance |
| Metricas de latencia por endpoint | Media | Media | Diagnosticar red/rate limits |
| Reducir scans redundantes | Baja | Media | Ciclos mas rapidos |

## Refactor

| Item | Prioridad | Complejidad | Impacto esperado |
|---|---|---:|---|
| Extraer motor de ciclo de `bot.py` | Media | Alta | Mantenimiento mas simple |
| Separar partial TP en modulo propio | Baja | Media | Reducir tamano de `bot.py` |
| Abstraer persistencia de estado | Media | Media | Migraciones mas seguras |
| Unificar nombres de campos de posicion | Alta | Media | Menos bugs por claves inconsistentes |
