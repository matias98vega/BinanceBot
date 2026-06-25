# Future Work

Este documento propone lineas de evolucion. No implica cambios implementados ni ajustes de estrategia.

## Observabilidad

| Mejora | Dificultad | Beneficio esperado | Prioridad |
|---|---:|---|---:|
| Alertas automaticas sobre resultado de `validate_observability.py` | Baja | Convertir validacion local en senal operativa periodica | Alta |
| Log estructurado para errores de Binance | Media | Separar fallos de red, rechazos de orden y errores logicos | Alta |
| Integrar `healthcheck.py` con cron/notificaciones | Baja | Detectar degradacion local sin revisar manualmente | Alta |
| Metricas de latencia por endpoint Binance | Media | Identificar problemas de red o rate limits antes de afectar ordenes | Media |
| Alertas por divergencia entre `state.json` y exchange | Media | Detectar posiciones huerfanas o cierres externos con mayor precision | Alta |
| Rotacion/compresion de logs historicos | Baja | Evitar archivos crecientes sin control | Media |

## Backtesting

| Mejora | Dificultad | Beneficio esperado | Prioridad |
|---|---:|---|---:|
| Replay offline de `market.score_long` y `market.score_short` con klines historicas | Alta | Evaluar filtros actuales sin operar en vivo | Alta |
| Dataset historico local de klines por simbolo/timeframe | Media | Reducir dependencia de API y acelerar simulaciones | Alta |
| Simulador de ejecucion con fees, slippage y minNotional | Alta | Backtests mas cercanos al comportamiento real | Alta |
| Comparacion live-vs-backtest usando `trade_analytics.jsonl` | Media | Validar si la simulacion representa la operacion real | Media |
| Backtest separado para guardian/stale/partial TP | Alta | Medir impacto de salidas sin tocar estrategia live | Media |

## Dashboard

| Mejora | Dificultad | Beneficio esperado | Prioridad |
|---|---:|---|---:|
| Dashboard local CSV/JSONL de performance | Media | Ver PnL, win rate, PF y drawdown sin revisar archivos | Alta |
| Vista de posiciones abiertas desde `state.json` | Baja | Estado operativo rapido | Alta |
| Panel de filtros por trade | Media | Entender por que se tomo o descarto una entrada | Media |
| Graficos por market regime, hora, simbolo y direccion | Media | Detectar patrones de rendimiento | Media |
| Export HTML estatico diario | Baja | Reporte facil de compartir/archivar | Media |

## Optimizacion

| Mejora | Dificultad | Beneficio esperado | Prioridad |
|---|---:|---|---:|
| Analisis de sensibilidad de parametros sobre backtests | Alta | Saber que parametros son fragiles | Media |
| Optimizacion walk-forward offline | Alta | Evitar overfitting al ajustar reglas | Media |
| Ranking de filtros por impacto historico | Media | Identificar filtros redundantes o costosos | Media |
| Cache local de klines por ciclo | Media | Reducir llamadas repetidas y rate limits | Alta |
| Separar configuracion por entorno live/dry-run/backtest | Media | Reducir riesgo operacional al experimentar | Alta |

## Gestion de riesgo

| Mejora | Dificultad | Beneficio esperado | Prioridad |
|---|---:|---|---:|
| Registro estructurado de exposicion por wallet y direccion | Baja | Medir riesgo real por ciclo | Alta |
| Auditoria de consistencia estado-exchange antes de operar | Media | Evitar operar sobre estado desactualizado | Alta |
| Reporte de max drawdown diario/semanal | Baja | Medir riesgo acumulado | Alta |
| Simulador de escenarios extremos BTC | Alta | Entender comportamiento ante pumps/dumps | Media |
| Politicas documentadas de recuperacion manual | Baja | Reducir improvisacion ante fallos | Alta |

## Machine Learning

| Mejora | Dificultad | Beneficio esperado | Prioridad |
|---|---:|---|---:|
| Dataset tabular desde `trade_analytics.jsonl` + features de entrada | Media | Base para modelos predictivos sin tocar live | Media |
| Modelo offline para probabilidad de TP/SL | Alta | Evaluar si los features actuales contienen senal | Baja |
| Clasificador de regimen de mercado offline | Alta | Comparar contra regla BTC EMA20/EMA50 | Baja |
| Deteccion de outliers de simbolo/volatilidad | Media | Identificar condiciones anormales antes de operar | Media |
| Evaluacion de feature importance | Media | Entender que indicadores explican resultados | Media |

## IA

| Mejora | Dificultad | Beneficio esperado | Prioridad |
|---|---:|---|---:|
| Resumen diario automatico de performance y errores | Baja | Mejor lectura operativa sin revisar logs crudos | Alta |
| Analisis post-trade narrativo basado en analytics | Media | Explicar cada trade con contexto y resultado | Media |
| Asistente de diagnostico para fallos de Binance/API | Media | Reducir tiempo de investigacion | Media |
| Generacion de reportes semanales con hipotesis a validar | Media | Guiar investigacion sin cambiar estrategia automaticamente | Media |
| Recomendador offline de experimentos de backtest | Alta | Priorizar pruebas futuras con datos historicos | Baja |

## Orden sugerido de trabajo

1. Healthcheck de cron, integridad de JSONL y auditoria estado-exchange.
2. Dataset historico local y replay offline de scoring.
3. Dashboard basico de `state.json`, `trades_log.txt` y `trade_analytics.jsonl`.
4. Backtester con fees/slippage/minNotional.
5. Analisis de sensibilidad y reportes por regimen/simbolo/hora.
6. Modelos offline solo como herramienta de investigacion, nunca conectados a ejecucion live sin fase separada.
