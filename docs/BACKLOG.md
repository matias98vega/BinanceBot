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
| Formalizar releases historicos por fix con rangos temporales precisos | Alta | Media | Permitir excluir o marcar datos segun capacidades/bugs de cada version |
| Implementar modo write auditable en `repair_data_quality.py` con backup/checksum | Alta | Alta | Sanear deuda historica sin perder trazabilidad |
| Revisar plan dry-run `trade-gap` para `short_WLDUSDT_1782763085` en VPS antes de cualquier reparacion | Alta | Media | Resolver deuda historica sin fabricar eventos de apertura |
| Ampliar cobertura de Decision Timeline | Media | Media | Registrar filtros finos y motivos adicionales sin depender de journalctl |
| Implementar FakeBinanceClient | Alta | Media | Tests mas confiables sin patching disperso |
| Implementar ReplayBinanceClient | Media | Alta | Reproducir sesiones historicas sin tocar Binance |
| Implementar PaperBinanceClient | Media | Alta | Simular ordenes sin riesgo operativo |
| Implementar ShadowBinanceClient | Media | Alta | Comparar comportamiento alternativo sin afectar live |
| Retirar o migrar `auto_loop.py` legacy | Media | Alta | Eliminar stack Binance standalone fuera del bot modular |
| Polling de balance post-fill para Long Spot | Media | Media | Reducir carreras tras compra Spot |
| Auditoria de permisos API en setup | Media | Baja | Detectar claves sin permisos adecuados |
| Playbooks de recuperacion manual | Media | Baja | Reducir improvisacion ante fallos |
| Rotacion de `data/history/*.jsonl` | Media | Baja | Evitar crecimiento indefinido de memoria historica |
| Validacion periodica de consistencia `stats.json` vs JSONL | Media | Baja | Detectar indices stale o corruptos |
| Validacion de freshness de `insights.json` vs `stats.json` | Media | Baja | Evitar conclusiones derivadas de estadisticas stale |
| Enriquecer Trade Inspector con mas motivos de filtros | Media | Media | Explicar mejor rechazos y cambios relevantes |
| Validar fallback Guardian tras rechazo de SL nativo | Media | Media | Confirmar proteccion software antes de clasificar alertas |
| Validar completitud del Feature Store | Media | Media | Medir campos faltantes antes de usarlo en aprendizaje |

## UI

| Item | Prioridad | Complejidad | Impacto esperado |
|---|---|---:|---|
| Vista compacta de timeline en dashboard | Alta | Media | Diagnostico rapido |
| Vista compacta de insights en dashboard | Media | Media | Mostrar conclusiones sin depender de Telegram |
| Vista visual de Trade Inspector | Media | Media | Auditar un trade completo desde navegador |
| Vista de capacidad real por wallet | Alta | Baja | Entender slots disponibles |
| Separar paneles de capital, riesgo y sistema | Media | Media | Lectura mas clara |
| Filtros por simbolo/direccion/regimen | Media | Media | Analisis mas util |

## Telegram

| Item | Prioridad | Complejidad | Impacto esperado |
|---|---|---:|---|
| Paginacion y filtros avanzados para `/timeline` | Media | Media | Navegar historiales largos desde celular |
| Filtros avanzados para `/insights` | Media | Baja | Consultar conclusiones por categoria |
| Seleccion interactiva avanzada de Trade Inspector | Media | Media | Elegir trades desde Telegram sin escribir ids |
| Notificaciones de recovery/critical mas claras | Alta | Baja | Mejor respuesta operativa |
| Paginacion avanzada de rankings estadisticos Telegram | Media | Media | Navegar historiales largos sin mensajes extensos |
| Healthcheck resumido periodico configurable | Media | Media | Detectar degradacion sin revisar manualmente |
| Comandos de diagnostico no mutantes | Media | Media | Investigar sin tocar estado ni ordenes |

## Dashboard

| Item | Prioridad | Complejidad | Impacto esperado |
|---|---|---:|---|
| UI visual para `/api/timeline` | Media | Media | Exponer eventos cronologicos en el dashboard web |
| UI visual para `/api/insights` | Media | Media | Exponer conclusiones generadas por el Insights Engine |
| UI visual para `/api/trade/<id>` | Media | Media | Navegar timeline, capital y protecciones de un trade |
| Graficos PnL por dia/simbolo/direccion | Media | Media | Mejor analisis de performance |
| Tabla de rechazos frecuentes | Media | Media | Detectar filtros dominantes |
| Panel de freshness de archivos | Media | Baja | Identificar observabilidad stale |

## Estadisticas

| Item | Prioridad | Complejidad | Impacto esperado |
|---|---|---:|---|
| Win rate y profit factor por regimen | Alta | Media | Medir si el contexto BTC agrega valor |
| Drawdown diario/semanal | Alta | Baja | Medir riesgo acumulado |
| Drawdown detallado intradia con curva de equity completa | Media | Media | Mejor lectura de riesgo temporal |
| Distribucion de tiempo en posicion | Media | Baja | Evaluar stale exits |
| Analisis de SL/TP distance vs resultado | Media | Media | Entender sensibilidad de salidas |
| Insights por periodo con muestras minimas configurables | Media | Baja | Reducir falsos positivos en alertas |

## Machine Learning Futuro

| Item | Prioridad | Complejidad | Impacto esperado |
|---|---|---:|---|
| Dataset tabular desde analytics/snapshots | Media | Media | Base para investigacion offline |
| Export tabular desde Feature Store | Media | Media | Preparar Shadow Mode, Replay y Auto Optimizer |
| Migracion futura de JSONL historico a SQLite | Media | Media | Consultas mas rapidas sin cambiar el contrato de escritura |
| Modelo offline de probabilidad TP/SL | Baja | Alta | Evaluar senal predictiva |
| Feature importance de indicadores | Media | Media | Priorizar filtros utiles |
| Deteccion de outliers de simbolo | Media | Media | Alertar condiciones anormales |

## Adaptacion Dinamica

| Item | Prioridad | Complejidad | Impacto esperado |
|---|---|---:|---|
| Ajustes por regimen solo en simulacion | Media | Alta | Evitar cambios live sin evidencia |
| Cooldown dinamico por simbolo | Baja | Media | Ajustar proteccion a comportamiento historico |
| Risk budget por drawdown reciente | Media | Alta | Gestion de riesgo mas profesional |
| Shadow Mode sobre Feature Store | Media | Alta | Evaluar cambios sin tocar decisiones live |
| Auto Optimizer offline | Baja | Alta | Sugerir parametros futuros usando evidencia historica |

## Testing

| Item | Prioridad | Complejidad | Impacto esperado |
|---|---|---:|---|
| Tests de cierre preventivo BTC | Alta | Media | Cubrir flujo de riesgo critico |
| Tests de state-vs-exchange reconciliation | Alta | Alta | Evitar operar sobre estado falso |
| Tests de Telegram pages | Media | Media | Evitar regresiones UX |
| Tests dashboard APIs | Media | Baja | Mantener contrato read-only |
| Tests de rotacion JSONL | Media | Baja | Evitar crecimiento sin control |
| Tests de migracion JSONL a SQLite | Baja | Media | Preparar evolucion de storage |

## Performance

| Item | Prioridad | Complejidad | Impacto esperado |
|---|---|---:|---|
| Cache local de klines por ciclo | Media | Media | Menos llamadas Binance |
| Metricas de latencia por endpoint | Media | Media | Diagnosticar red/rate limits |
| Reducir scans redundantes | Baja | Media | Ciclos mas rapidos |

## Refactor

| Item | Prioridad | Complejidad | Impacto esperado |
|---|---|---:|---|
| Extraer orquestacion de entradas de `cycle_runner.py` | Media | Alta | Separar seleccion/apertura de la coordinacion del ciclo |
| Abstraer persistencia de estado | Media | Media | Migraciones mas seguras |
| Unificar nombres de campos de posicion | Alta | Media | Menos bugs por claves inconsistentes |
