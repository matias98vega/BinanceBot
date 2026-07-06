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

**Solucion actual:** antes de recrear OCO para un huerfano Spot o una LONG en recovery sin OCO, `residuals.py` calcula cantidad redondeada, precio redondeado, `estimated_value` y los nocionales reales de las patas OCO: `limit_notional = qty * price` y `stop_notional = qty * stopLimitPrice`. Binance puede rechazar `NOTIONAL` aunque `estimated_value` parezca suficiente, porque la pata stop-limit suele quedar por debajo del precio actual. La decision de protegible usa `min_leg_notional >= min_notional`; si cualquier pata queda bajo el minimo, no se envia POST OCO y el residual se registra como `unprotectable_residual` con motivo `oco_leg_below_min_notional`. `audit_pipeline.py`, `longs._recolocar_oco()` y `position_lifecycle.recolocar_oco_long()` usan ese helper antes de cualquier POST OCO de recuperacion. El estado guarda simbolo, asset, cantidad, valor estimado, minimo requerido, `rounded_qty`, precios OCO, nocional por pata, pata limitante, motivo, timestamps, contador de alertas y accion sugerida. Tambien se registra un evento `spot_residual_unprotectable` en Timeline.

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

## Dust / Saldos Minimos

**Problema:** despues de compras Spot, fees, redondeos o cierres parciales pueden quedar saldos pequenos. Algunos son residuales protegibles, otros son residuales no protegibles por filtros de Binance y otros son simplemente dust.

**Definiciones:**

- Residual protegible: saldo Spot libre, sin OCO, con cantidad y notional suficientes despues de aplicar `stepSize`, `tickSize`, `minQty` y `minNotional`; puede recibir OCO de recuperacion.
- Residual no protegible: saldo Spot libre, sin OCO, que parece una posicion huerfana pero queda por debajo de `NOTIONAL`, `MIN_NOTIONAL` o `LOT_SIZE` despues de redondeos. Se registra como `unprotectable_residual` y no se envia OCO.
- Dust: saldo muy pequeno sin posicion activa ni orden abierta. Un saldo como `0.00131 SOL` no requiere OCO porque su valor efectivo esta por debajo del minimo operativo de Binance y no representa una posicion protegible.

**Solucion actual:** `residuals.py` clasifica residuales no protegibles, persiste `data/history/residuals_status.json`, emite alertas humanas con throttling y conserva logs de produccion como `RESIDUAL UNPROTECTABLE` y `RESIDUAL STATUS WRITE`. La instrumentacion temporal de diagnostico del flujo OCO fue retirada para evitar ruido.

**Dust cleaner existente:** el repositorio conserva un flujo de limpieza de dust conectado al ciclo actual. `utils.clean_dust(dry_run=True)` detecta saldos no protegidos por `DUST_PROTECTED`, exige un valor total minimo (`DUST_MIN_VALUE_USD`) y usa el endpoint Binance `/sapi/v1/asset/dust`, que convierte activos pequenos a BNB. `BinanceClient.clean_dust()` delega en ese helper y `audit_pipeline.maybe_clean_dust()` lo invoca desde `CycleRunner` con frecuencia semanal controlada por `DUST_CLEAN_DAY` y estado local.

**Compuerta explicita:** `DRY_RUN` controla el trading, no la limpieza de dust. La conversion real de dust requiere `AUTO_CLEAN_DUST=True` y `DUST_CLEAN_DRY_RUN=False`. Por defecto `AUTO_CLEAN_DUST=False` y `DUST_CLEAN_DRY_RUN=True`, por lo que el ciclo puede omitir o simular la limpieza, pero no convertir automaticamente.

**Riesgos:** aunque el helper existe y es reutilizable, una conversion real depende de permisos de dust conversion en Binance, cambia balances Spot y puede afectar auditoria contable. Por eso queda desactivada por defecto y separada del modo real/simulado de trading.

**Mejora futura:** crear un `Dust Manager` pasivo por defecto. Sus responsabilidades serian detectar dust sin posicion activa ni orden abierta, verificar valor estimado y elegibilidad de conversion/venta, persistir estado, alertar, y ejecutar limpieza solo si una configuracion explicita lo habilita.

## Reconciliacion Futures Observada

**Problema:** Binance puede mantener posiciones Futures abiertas aunque el historial interno tenga un cierre total o aunque `state.json` ya no conserve una posicion gestionada. Esto consume margen, bloquea rebalance y deja riesgo operativo si no hay ordenes abiertas de proteccion.

**Decision actual:** `futures_reconciliation.py` clasifica posiciones Futures observadas desde Binance de forma pasiva. No cierra posiciones, no modifica payloads y no cambia la logica normal de entradas/salidas. Persiste `data/history/futures_reconciliation_status.json` con posicion, margen, estado de gestion interna, presencia de open orders, clasificaciones y severidad. Ese archivo es la fuente reconciliada para Home, Capital y Posiciones.

**Fuente de datos:** la reconciliacion usa posiciones observadas desde Binance, preferentemente el payload crudo de `futures_position_risk()` con `positionAmt != 0`. Si solo existe el snapshot normalizado usado por Telegram, tambien acepta `quantity` + `side`. Nunca depende solo de `state.json`.

**Clasificaciones:** `observed_futures_position` significa que Binance reporta `positionAmt != 0`. `managed_futures_position` existe tambien en `state.json` con metadata suficiente de lifecycle. `unmanaged_futures_position` y `orphan_futures_position` indican que Binance ve la posicion pero el bot no tiene lifecycle confiable. `unprotected_futures_position` indica que no hay open orders. `desynced_closed_but_open_on_exchange` indica que el historial tiene cierre total pero Binance sigue abierto. `stale_observed_futures_position` marca posiciones observadas con antiguedad estimada mayor a 24h.

**Parsing Binance:** shorts con `notional` negativo siguen siendo posiciones abiertas si `abs(positionAmt) > 0`. Para totales y resumen se usa `abs(notional)`, preservando `position_amt` firmado para entender el lado real.

**Estado reconciliado:** `ALINEADO` solo aplica si `observed_count <= allowed_count` cuando hay limite disponible, y ademas `unmanaged_count`, `orphan_count`, `unprotected_count` y `desynced_count` son cero. Si hay exceso contra capacidad o posiciones no gestionadas, el estado explicita ambos motivos.

**Presentacion Telegram:** Home y Capital usan formato compacto cuando la reconciliacion esta sana: `Shorts: abiertas/permitidas` y `Futures: usado / real`. El bloque expandido con observadas, gestionadas, permitidas, sin proteccion y estado aparece solo si hay riesgo: exceso contra la capacidad operativa, posiciones no gestionadas/huerfanas/sin proteccion/desincronizadas o estado no alineado. `allowed_count` debe venir de la misma politica operativa que usa el ciclo para decidir capacidad de shorts; en Bull, si el ciclo permite cero shorts, Telegram muestra `Shorts: 0/0`. Esto es solo UI/observabilidad y no modifica trading, recovery ni rebalance.

**Hallazgo stale/24h:** la regla stale opera sobre posiciones activas en `state.json`. Si una posicion residual queda fuera de `state.json` o fue registrada como cerrada antes de confirmar `positionAmt=0`, el lifecycle normal ya no la recorre y la regla stale no puede cerrarla. Por eso esta iteracion solo alerta y clasifica.

**Riesgo:** una posicion Futures sin TP/SL/reduce-only abierta puede seguir acumulando PnL no realizado y bloquear transferencias. El bot no debe cerrarla automaticamente sin un flujo explicito de recovery porque podria cerrar una posicion que requiere revision humana o conciliacion de historial.

**Recovery manual:** `futures_recovery.py` implementa un flujo read-confirm-execute para posiciones Futures huerfanas/no gestionadas/desincronizadas. `/futures_recovery_preview` lista candidatas y la orden propuesta sin enviar nada. `/futures_recovery_close SYMBOL CONFIRM` cierra solo ese simbolo si supera pre-checks.

**Pre-checks:** el simbolo debe existir en `futures_reconciliation_status.json`, no debe estar gestionado activamente, debe tener clasificacion de recovery (`unmanaged`, `orphan`, `unprotected` o `desynced`), debe incluir confirmacion literal `CONFIRM`, se reconsulta `futures_position_risk(symbol)` antes de cerrar y se valida cantidad contra `stepSize/minQty`.

**Orden de recovery:** SHORT (`positionAmt < 0`) se cierra con `BUY MARKET reduceOnly=true`; LONG (`positionAmt > 0`) con `SELL MARKET reduceOnly=true`. `reduceOnly` es obligatorio para impedir abrir una posicion nueva por error.

**Riesgos restantes:** si Binance rechaza por precision, minimo, margen o estado de posicion, el recovery no reintenta ni fuerza cierre. Registra timeline con `code/msg/raw_body` y deja el caso para revision manual.

**Mejora futura:** agregar una segunda capa opcional de recovery con aprobacion persistente, reporte post-cierre y reconciliacion automatica del estado una vez confirmado `positionAmt=0`.

## Auditoria Local de Datos

**Problema:** los historicos JSON/JSONL pueden degradarse con campos faltantes, lineas corruptas, timestamps invalidos o relaciones incompletas entre trades, features, timeline, rebalance y ledger.

**Solucion actual:** `audit_data_quality.py` es una herramienta local de solo lectura que valida archivos runtime/historicos, resume errores criticos, warnings, campos faltantes, completitud y recomendaciones. Devuelve exit code `1` si encuentra errores criticos y `0` cuando solo hay warnings o todo esta correcto.

**Ventajas:** permite auditar calidad antes de usar los datos para Analytics avanzado, PnL ajustado o aprendizaje futuro sin tocar estrategia ni estado operativo.
