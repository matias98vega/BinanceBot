# Design Notes

Este documento registra decisiones de diseno importantes. Su objetivo es preservar contexto: que problema se resolvio, que alternativas existian y que costo se acepto.

## Arquitectura Modular

**Problema:** el bot necesita operar Spot, Futures, observabilidad, Telegram y dashboard sin mezclar responsabilidades.

**Alternativas consideradas:** un script unico, watchers separados por simbolo, o modulos especializados.

**Solucion actual:** `bot.py` orquesta; `market.py` decide candidatos; `longs.py` y `shorts.py` ejecutan/gestionan posiciones; `sl_guardian.py` protege; `utils.py` centraliza Binance/estado/helpers.

**Ventajas:** facilita auditar entradas, salidas y guardrails por separado.

**Desventajas:** `bot.py` aun concentra demasiada logica de ciclo y cierre.

**Mejoras futuras:** extraer un motor de ciclo y un servicio de reconciliacion state-vs-exchange.

## UX Telegram y Observabilidad

**Problema:** la misma informacion podia verse con pequenas diferencias entre Home, Estadisticas, Timeline e Insights.

**Alternativas consideradas:** duplicar calculos en cada pagina, leer siempre `bot_state.json`, o usar los indices pasivos ya existentes como fuente de presentacion.

**Solucion actual:** Home usa Analytics Engine como fuente unica visible para PnL, igual que Estadisticas. Estadisticas combina Analytics para metricas historicas/cerradas con el estado vivo del bot para posiciones abiertas; "Abiertos" representa posiciones activas actuales, no trades historicos abiertos. Home, Capital y Diagnostico muestran el regimen actual, BTC 4h, precio BTC y modo direccional leyendo `bot_state.market`, sin recalcular contexto ni tocar decisiones. Timeline conserva categorias tecnicas en JSONL pero presenta etiquetas localizadas al usuario. Insights filtra conclusiones comparativas cuando la muestra es baja y muestra mensajes de muestra insuficiente.

**Ventajas:** reduce contradicciones visuales y evita conclusiones prematuras con pocos trades.

**Desventajas:** si `stats.json` esta desactualizado, Home y Estadisticas compartiran el mismo desfase hasta que Analytics lo reconstruya.

**Mejoras futuras:** mostrar freshness de `stats.json` en Home o Sistema si la edad del indice supera un umbral.

## Guardian Independiente

**Problema:** el ciclo principal corre cada pocos minutos; un SL no deberia depender solo de ese intervalo.

**Alternativas:** solo SL nativo, solo OCO, o guardian separado.

**Solucion actual:** `sl_guardian.py` corre como proceso liviano independiente. Para Long Spot no actua si hay OCO; para posiciones sin proteccion puede cerrar con MARKET. Para Shorts puede complementar SL software/nativo.

**Ventajas:** reduce ventana de exposicion si el ciclo principal falla o demora.

**Desventajas:** requiere disciplina para no duplicar cierres y para sincronizar `state.json`.

**Mejoras futuras:** reconciliacion previa contra exchange antes de cerrar y estado explicito de `already_closed`.

## Rebalance Spot/Futures

**Problema:** el capital debe seguir el regimen de mercado sin abrir/cerrar trades por si mismo.

**Alternativas:** asignacion fija, rebalance manual, rebalance automatico por tendencia BTC.

**Solucion actual:** `rebalance.py` calcula targets segun contexto bullish/bearish/neutral y mueve USDT entre wallets respetando posiciones abiertas, minimo de transferencia, reserva opcional y un pequeno `REBALANCE_TRANSFER_BUFFER_USDT` sobre el monto final. Si Binance rechaza una transferencia con `code=-5013` por saldo insuficiente, el bot reintenta una sola vez descontando otro buffer. Si una transferencia falla, persiste `data/history/rebalance_status.json` con direccion, monto, intentos, HTTP status, code/msg Binance, raw body seguro, endpoint, metodo y payload sanitizado. Si una transferencia queda pendiente sin intento porque existe una condicion de bloqueo, el mismo archivo registra `pending_reason`, `blocked_reason`, `last_check`, `last_attempt` y `attempts` para evitar estados mudos en Telegram/Dashboard. Si el estado queda pendiente pero los balances reales ya estan alineados dentro de `REBALANCE_ALIGNMENT_TOLERANCE_USDT`, el estado se reconcilia como resuelto sin cambiar el target teorico.

**Ventajas:** separa asignacion de capital de senales de entrada y permite diagnosticar fallos recurrentes desde Telegram/Dashboard sin leer journalctl.

**Desventajas:** depende de balances correctos y de que las transferencias Binance respondan sin retrasos. El buffer puede dejar una fraccion minima sin mover, pero reduce rechazos por redondeos, locks temporales o diferencias entre saldo libre visible y saldo realmente transferible. El estado persistente es diagnostico, no una cola de reintentos.

**Mejoras futuras:** auditoria post-transfer y simulacion dry-run de rebalance.

**Recuperacion:** cuando una transferencia posterior se ejecuta correctamente, `rebalance_status.json` se limpia automaticamente (`pending=false`). Si el fallo persiste, el contador de intentos acumulados y el ultimo motivo Binance se mantienen visibles. Si no hay intento porque el capital esta bloqueado por posiciones activas, reserva o capital manager, el ultimo check y el bloqueo se mantienen visibles. Si una transferencia manual o externa deja Spot/Futures dentro de la tolerancia, el estado se marca con `resolved_reason=capital_already_aligned` y se registra un evento de Timeline.

**Observabilidad Futures:** `Futures Wallet` es el capital real de la wallet Futures (`totalWalletBalance` mas uPnL cuando aplica para equity). `Position Margin` es el capital comprometido en posiciones abiertas (`totalPositionInitialMargin`) y se muestra como Futures usado. `Available Balance` es el saldo libre transferible/operable y sigue siendo la base para decidir si una transferencia puede ejecutarse. `Pending Amount` es el desbalance real contra el target teorico. `Transferable Amount` es la parte de ese desbalance que puede moverse ahora despues de considerar saldo libre, reserva y buffer. Telegram muestra estos conceptos separados para no confundir un bloqueo por margen con un rebalance alineado.

## Capital Manager

**Problema:** sizing y validacion de capital pueden divergir si cada modulo calcula maximos por su cuenta.

**Alternativas:** validaciones locales por modulo, constantes fijas por trade, o helper compartido.

**Solucion actual:** `capital_manager.max_margin_per_position(...)` es la fuente comun para margen maximo por operacion: capital usable por `BOT_MAX_EXPOSURE_PERCENT / max_positions`.

**Ventajas:** evita contradicciones entre sizing y guardrail.

**Desventajas:** aun existen variables deprecated de capital que deben retirarse con cuidado.

**Mejoras futuras:** eliminar `BOT_MAX_POSITION_PERCENT` cuando no quede compatibilidad pendiente.

## Capital Ledger

**Problema:** el capital total puede cambiar por depositos, retiros, rebalanceos internos, comisiones o funding. Si esos movimientos se mezclan con PnL, un deposito manual podria parecer rendimiento de trading.

**Alternativas consideradas:** corregir PnL directamente en Analytics, inferir depositos por diferencias de balance, o crear una capa contable separada.

**Solucion actual:** `capital_ledger.py` introduce `data/history/capital_ledger.jsonl` como ledger append-only de movimientos de capital. Registra tipos explicitos como `external_deposit`, `external_withdrawal`, `rebalance`, `realized_pnl`, `commission` y `funding_fee`, con API dedicada para escribir y leer sin acoplar el resto del bot al formato JSONL. `capital_accounting.py` queda por encima del ledger y centraliza la interpretacion contable: depositos/retiros acumulados, flujos externos netos, comisiones, funding, PnL realizado y helpers preliminares de equity/PnL/ROI ajustados. `analytics_engine.py` consume esos resultados mediante `CapitalAccounting` y expone metricas adicionales sin modificar las estadisticas historicas existentes.

**Ventajas:** separa hechos contables de calculos derivados. El ledger registra movimientos; accounting interpreta esos movimientos; Analytics consume resultados contables y no lee directamente el JSONL. Las metricas actuales de Analytics se mantienen como PnL historico observado, mientras las metricas ajustadas permiten separar capital aportado por el usuario del rendimiento generado por trading.

**Desventajas:** esta primera etapa no detecta depositos automaticamente; requiere registros explicitos o integraciones futuras.

**Formulas:** `Adjusted Equity = current_equity - external_deposits + external_withdrawals`. `Adjusted PnL = Adjusted Equity - starting_equity`. `Adjusted ROI = Adjusted PnL / starting_equity * 100`. Los depositos externos se restan porque no son rendimiento; los retiros se suman de vuelta porque reducen equity actual sin representar perdida de trading. Comisiones, funding y realized PnL quedan disponibles como componentes contables separados para reportes futuros.

**Presentacion en Telegram:** `/capital` muestra una seccion de contabilidad con depositos externos, retiros, flujo neto, equity ajustado, PnL ajustado y ROI ajustado. `/stats` y `Resumen General` muestran un bloque resumido de capital ajustado junto a las metricas actuales, sin reemplazarlas. Telegram consume exclusivamente `analytics_engine`; no lee el ledger ni la capa accounting directamente. Si faltan equity actual o baseline, las metricas ajustadas se muestran como `No disponible`.

**Mejoras futuras:** reconciliar balances contra Binance, registrar comisiones/funding desde eventos reales y exponer PnL ajustado en Telegram/Dashboard una vez validada la contabilidad.

## Scoring

**Problema:** seleccionar candidatos con multiples senales sin cambiar manualmente cada ciclo.

**Alternativas:** lista fija de simbolos, filtro simple por RSI/EMA, scoring multifactor.

**Solucion actual:** `market.score_long` y `market.score_short` combinan tendencia, RSI, MACD, ATR, volumen, correlacion y contexto BTC. El scan elige el candidato con mejor score/ATR entre los aprobados.

**Ventajas:** permite explicar rechazos y ordenar candidatos.

**Desventajas:** es dificil medir el aporte real de cada filtro sin backtesting.

**Mejoras futuras:** ranking historico de filtros, backtest offline y feature importance.

## Cooldown

**Problema:** tras un SL, reentrar inmediatamente en el mismo simbolo puede repetir un mal setup.

**Alternativas:** cooldown global, blacklist permanente, cooldown por simbolo.

**Solucion actual:** `cooldown_symbols` persiste expiraciones por simbolo. El bot migra formato legacy si hace falta.

**Ventajas:** reduce reentradas impulsivas sin bloquear todo el sistema.

**Desventajas:** depende de que cierres y guardian registren correctamente los SL.

**Mejoras futuras:** cooldown dinamico por volatilidad, regimen o racha por simbolo.

## TP y SL Dinamicos

**Problema:** TP/SL fijos ignoran volatilidad del activo.

**Alternativas:** porcentajes fijos, ATR, niveles tecnicos.

**Solucion actual:** TP y SL se derivan de ATR y de distancias minimas configuradas. Shorts pueden usar SL nativo si esta habilitado; Longs usan OCO Spot.

**Ventajas:** la distancia se adapta a volatilidad.

**Desventajas:** ATR puede expandirse en condiciones anormales y generar stops amplios o invalidos.

**Mejoras futuras:** evaluar sensibilidad de ATR por regimen y simular impacto antes de tocar live.

## SL Nativo Futures y Fallback

**Problema:** Binance puede rechazar el `STOP_MARKET` nativo de Futures con HTTP 400 aunque la posicion siga cubierta por Guardian software.

**Alternativas:** tratar todo rechazo como error critico, silenciarlo, o clasificarlo segun exista fallback.

**Solucion actual:** si falla el SL nativo pero Guardian queda activo, se registra WARNING operativo y se conserva el detalle tecnico en logs. Solo se eleva a CRITICAL si no queda fallback activo.

**Ventajas:** evita alarmas falsas de posicion desprotegida sin ocultar el diagnostico tecnico.

**Desventajas:** requiere que Guardian siga siendo una garantia operativa valida.

**Mejoras futuras:** reconciliacion explicita de fallback activo antes de cada alerta.

## Long Spot OCO y Recovery

**Problema:** despues de comprar Spot, fees/redondeos/filtros pueden dejar menos balance libre que la cantidad teorica. Si OCO o emergency sell usan la cantidad teorica, Binance puede rechazar con `-2010`.

**Alternativas:** confiar en `executedQty`, descontar fee estimado, o consultar balance real.

**Solucion actual:** despues del BUY se consulta balance real del asset, se ajusta por stepSize/minQty/minNotional y se usa esa cantidad para OCO, retry OCO, emergency sell y recovery.

**Ventajas:** evita intentar vender/proteger mas asset del disponible.

**Desventajas:** agrega llamadas privadas adicionales y depende de que el balance se actualice rapido.

**Mejoras futuras:** polling corto de balance post-fill y mas detalle de recovery en timeline.

## Residuales Spot No Protegibles

**Problema:** una posicion Spot huerfana puede quedar libre en balance, sin OCO y sin orden abierta, pero con valor efectivo inferior al minimo que Binance permite para una OCO despues de aplicar `stepSize`, `tickSize` y `MIN_NOTIONAL`/`NOTIONAL`. En ese caso Binance rechaza con `Filter failure: NOTIONAL` o restricciones equivalentes.

**Alternativas:** seguir reintentando OCO cada ciclo, vender automaticamente el residual, ignorarlo, o clasificarlo como estado conocido.

**Solucion actual:** antes de recrear OCO para un huerfano Spot o una LONG en recovery sin OCO, `residuals.py` calcula cantidad redondeada, precio redondeado y notional efectivo. `audit_pipeline.py`, `longs._recolocar_oco()` y `position_lifecycle.recolocar_oco_long()` usan ese helper antes de cualquier POST OCO de recuperacion. Si no alcanza el minimo, no envia la orden a Binance y registra el residual como `unprotectable_residual` en `data/history/residuals_status.json`. El estado guarda simbolo, asset, cantidad, valor estimado, minimo requerido, motivo, timestamps, contador de alertas y accion sugerida. Tambien se registra un evento `spot_residual_unprotectable` en Timeline.

**Ventajas:** evita HTTP 400 repetitivos, reduce ruido operativo y muestra una accion manual clara sin tocar estrategia ni flujo normal de ordenes.

**Desventajas:** el residual queda sin proteccion automatica hasta que el usuario lo venda manualmente o acumule saldo suficiente para cumplir el minimo de Binance.

**Por que no se vende automaticamente:** este cambio es observabilidad/hardening pasivo. Vender residuos automaticamente implicaria nueva logica operativa de salida y debe evaluarse por separado.

**Mejoras futuras:** comando read-only en Telegram para listar residuales, opcion manual explicita para limpiar polvo/residuales y reconciliacion contra conversion de dust de Binance.

## Snapshots de Decision

**Problema:** entender por que el bot entro o rechazo candidatos sin leer logs crudos.

**Alternativas:** logs humanos, JSONL estructurado, base de datos.

**Solucion actual:** `decision_snapshots.jsonl` guarda contexto, capital y candidatos accepted/rejected/skipped. Para lectura cronologica compacta, `decision_timeline.py` registra eventos en `data/history/timeline.jsonl`.

**Ventajas:** append-only, facil de analizar y de mostrar en dashboard/Telegram.

**Desventajas:** snapshots y timeline se complementan; hay que evitar duplicar ruido excesivo entre ambos.

**Mejoras futuras:** enriquecer motivos de filtros sin cambiar scoring.

## Decision Timeline

**Problema:** para auditar un ciclo era necesario reconstruir eventos desde `journalctl`, logs humanos, snapshots y analytics.

**Alternativas consideradas:** usar solo snapshots, enviar alertas Telegram por cada evento, o crear un JSONL historico consultable.

**Solucion actual:** `decision_timeline.py` registra eventos append-only en `data/history/timeline.jsonl` con nivel, categoria, simbolo/direccion opcional, mensaje y detalles sanitizados. El archivo rota al superar 5 MB preservando eventos recientes.

**Ventajas:** permite consultar eventos recientes desde Telegram (`/timeline`) y dashboard (`/api/timeline`) sin generar spam ni tocar estado operativo.

**Desventajas:** al ser pasivo, depende de que los modulos llamen al helper en los puntos relevantes. Todavia puede ampliarse la cobertura de filtros finos.

**Mejoras futuras:** UI dashboard dedicada, paginacion Telegram y filtros combinados por ciclo/simbolo/categoria.

## Insights Engine

**Problema:** las estadisticas agregadas explican numeros, pero no resumen automaticamente que conclusiones son relevantes para operar y mejorar el sistema.

**Alternativas consideradas:** calcular insights desde `trades.jsonl`, pedirlos directamente a Telegram, o derivarlos desde el indice estadistico.

**Solucion actual:** `insights_engine.py` consume exclusivamente `stats.json` mediante `analytics_engine`, genera `data/history/insights.json` y expone conclusiones estructuradas con tipo, categoria, prioridad, texto, datos utilizados y confianza.

**Ventajas:** mantiene una frontera clara: JSONL historico es fuente de verdad, `stats.json` es indice estadistico y `insights.json` es una vista interpretativa rapida para Telegram, dashboard y futuros asistentes GPT.

**Desventajas:** si `stats.json` esta stale, los insights tambien lo estaran; por eso no se usan para decisiones operativas.

**Mejoras futuras:** freshness check entre `stats.json` e `insights.json`, explicaciones GPT sobre cada insight y filtros por categoria/periodo.

## Telegram Read-Only

**Problema:** se necesita consultar estado desde el celular sin riesgo de operar accidentalmente.

**Alternativas:** comandos operativos, solo alertas, menu read-only.

**Solucion actual:** `telegram_commands.py` expone menu y paginas de estado, capital, posiciones, health, diagnostico, trades, snapshots, estadisticas, insights y timeline. No abre/cierra ordenes ni modifica `state.json`.

**Posiciones:** la pagina de posiciones separa dos fuentes read-only. Spot muestra posiciones gestionadas por el bot desde `state.json`, porque son las entradas Spot que el bot controla directamente. Futures muestra posiciones observadas desde Binance a traves de `bot_state.positions.short.observed`, alimentado por el mismo read-model que usan Home y Capital. Si una posicion Futures existe en ambas fuentes, se muestra una sola vez y se prioriza el dato observado del exchange.

**Presentacion compacta:** Posiciones se divide en `Spot` y `Futures`. Cada posicion ocupa tres lineas principales: identificacion/lado, PnL o notional, y protecciones o precios. Esto permite ver varias posiciones abiertas en un solo mensaje sin perder los datos operativos importantes. Si Binance no entrega un campo, Telegram muestra `No disponible` en vez de ocultar la posicion.

**Ventajas:** baja friccion operativa y menor riesgo.

**Desventajas:** depende de freshness de `bot_state.json` y archivos JSONL.

**Mejoras futuras:** paginacion de timeline, filtros combinados de insights y comandos de diagnostico sin mutacion.

## Dashboard Local

**Problema:** se necesita una vista web local sin exponer Binance ni credenciales.

**Alternativas:** dashboard externo, Flask completo, `http.server` simple.

**Solucion actual:** `dashboard/app.py` sirve HTML/CSS/JS y APIs read-only leyendo archivos locales.

**Ventajas:** simple, sin dependencias pesadas y sin conexion directa a Binance.

**Desventajas:** no tiene autenticacion propia; debe quedar en loopback o detras de proxy protegido.

**Mejoras futuras:** UI visual para timeline/insights, mas paneles de analitica y comparacion por regimen.

## Healthcheck y Preflight

**Problema:** antes de operar conviene detectar corrupcion de archivos, locks viejos o desalineacion local.

**Alternativas:** confiar en logs, checks manuales, o scripts automatizados.

**Solucion actual:** `healthcheck.py`, `validate_observability.py`, `preflight_check.py` y `post_cycle_check.py` revisan estado local sin abrir ordenes.

**Ventajas:** mejora seguridad operacional antes/despues de ciclos.

**Desventajas:** no reemplaza una reconciliacion completa contra exchange.

**Mejoras futuras:** integrarlo con Telegram y systemd health alerts.

## Logs y Observabilidad

**Problema:** los logs humanos no bastan para analisis y los errores Binance necesitan contexto.

**Alternativas:** solo `trades_log.txt`, JSONL append-only, sistema externo de logs.

**Solucion actual:** se mantienen logs humanos, analytics JSONL, snapshots JSONL, timeline JSONL y logging estructurado de HTTP Binance con payload seguro.

**Ventajas:** permite analisis local, dashboard y diagnostico de errores.

**Desventajas:** no hay rotacion uniforme de todos los archivos.

**Mejoras futuras:** retention policy uniforme y export automatico.

## Auditoria Local de Datos

**Problema:** los historicos JSON/JSONL pueden degradarse con campos faltantes, lineas corruptas, timestamps invalidos o relaciones incompletas entre trades, features, timeline, rebalance y ledger.

**Solucion actual:** `audit_data_quality.py` es una herramienta local de solo lectura que valida archivos runtime/historicos, resume errores criticos, warnings, campos faltantes, completitud y recomendaciones. Devuelve exit code `1` si encuentra errores criticos y `0` cuando solo hay warnings o todo esta correcto.

**Ventajas:** permite auditar calidad antes de usar los datos para Analytics avanzado, PnL ajustado o aprendizaje futuro sin tocar estrategia ni estado operativo.
