# Future Vision

Este documento describe hacia donde podria evolucionar BinanceBot. No autoriza implementacion ni cambios en estrategia live.

## Principio General

El proyecto debe evolucionar desde un bot operativo hacia una plataforma pequena pero seria de investigacion, ejecucion y observabilidad. La regla central: nada se conecta a live sin evidencia, tests y una fase separada de aprobacion.

## Adaptacion Dinamica

La adaptacion dinamica deberia usar datos historicos para ajustar comportamiento segun regimen, volatilidad, liquidez y drawdown. En el corto plazo debe existir solo como analisis offline. En el futuro podria sugerir ajustes, pero no aplicarlos automaticamente sin control.

Ideas:

- Riesgo por posicion dependiente de drawdown reciente.
- Cooldown por simbolo segun comportamiento historico.
- Score minimo segun regimen y volatilidad.
- Slots por direccion segun capital real y calidad de candidatos.

## Kits de Estrategias

El proyecto podria soportar multiples estrategias como "kits" aislados:

- Trend-following.
- Mean reversion.
- Breakout.
- Volatility contraction/expansion.
- Regime-specific strategies.

Cada kit deberia declarar:

- Universo de simbolos.
- Timeframes.
- Features requeridas.
- Reglas de entrada/salida.
- Risk budget.
- Contrato de backtest.

## Aprendizaje Basado en Estadisticas

Antes de ML, el proyecto necesita estadistica robusta:

- Win rate por regimen.
- Profit factor por simbolo/direccion.
- Drawdown por periodo.
- Tiempo medio hasta TP/SL.
- Rechazos por filtro.
- Sensibilidad a ATR, RSI, score y volumen.

Estas metricas deben guiar que experimentar, no cambiar live directamente.

## Deteccion Automatica de Regimen

El regimen actual esta basado en contexto BTC. Una evolucion podria comparar varios clasificadores:

- EMA20/EMA50 BTC.
- Momentum multi-timeframe.
- Volatilidad realizada.
- Breadth de mercado crypto.
- Correlacion promedio de alts con BTC.
- Modelo offline supervisado.

La salida deberia ser explicable: bullish, bearish, neutral, volatile, risk-off, chop.

## Optimizacion Continua

La optimizacion debe ser offline y reproducible:

- Backtests versionados.
- Walk-forward.
- Separacion train/test por tiempo.
- Penalizacion por overfitting.
- Reporte de sensibilidad.
- Comparacion contra baseline actual.

El objetivo no es encontrar parametros perfectos, sino identificar parametros robustos.

## Gestion Profesional de Riesgo

Lineas futuras:

- Max drawdown diario/semanal/mensual.
- Riesgo agregado por wallet.
- Riesgo por direccion.
- Riesgo por correlacion.
- Kill switch manual documentado.
- Politica de reduccion de riesgo tras errores operativos.
- Reconciliacion obligatoria state-vs-exchange antes de abrir nuevas posiciones.

## Portfolio Multi-Estrategia

Una version futura podria asignar capital entre estrategias:

- Cada estrategia tiene PnL, drawdown y exposure propios.
- Un risk manager global decide presupuesto.
- El portfolio impide exposiciones duplicadas por simbolo/direccion.
- Se comparan estrategias activas contra estrategias en shadow mode.

## Motor de Experimentacion

El motor de experimentacion deberia permitir:

- Ejecutar replay de datos historicos.
- Cambiar parametros sin tocar live.
- Guardar resultados reproducibles.
- Comparar versiones de estrategia.
- Generar reportes por experimento.

## Backtesting Automatico

Backtesting requerido antes de cambios mayores:

- Klines locales por simbolo/timeframe.
- Fees y slippage.
- Filtros Binance: stepSize, tickSize, minQty, minNotional.
- Latencia simulada.
- OCO/TP/SL/trailing/stale/partial.
- Rebalance y capital allocation.

## Optimizacion de Parametros

Debe hacerse con cuidado:

- No optimizar sobre pocos trades.
- No seleccionar solo por PnL.
- Priorizar robustez, max drawdown y estabilidad.
- Validar fuera de muestra.
- Registrar el set de parametros y la version del codigo.

## Comparacion Entre Versiones

Cada cambio de estrategia futuro deberia poder responder:

- Que version se compara contra cual.
- Que periodo se uso.
- Que simbolos/timeframes se incluyeron.
- Que metricas mejoran/empeoran.
- Que riesgos nuevos aparecen.
- Si el cambio se recomienda para live, dry-run o rechazo.
