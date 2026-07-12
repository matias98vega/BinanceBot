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

**Solucion actual:** Home usa `bot_state.pnl` como fuente prioritaria para PnL vivo, alineado con el ultimo ciclo operativo; si no existe, cae a Analytics Engine. Estadisticas mantiene Analytics para metricas historicas/cerradas y combina el estado vivo del bot para posiciones abiertas; "Abiertos" representa posiciones activas actuales, no trades historicos abiertos. Home, Capital y Diagnostico muestran el regimen actual, BTC 4h, precio BTC y modo direccional leyendo `bot_state.market`, sin recalcular contexto ni tocar decisiones. Timeline conserva categorias tecnicas en JSONL pero presenta etiquetas localizadas al usuario. Insights filtra conclusiones comparativas cuando la muestra es baja y muestra mensajes de muestra insuficiente.

**Ventajas:** reduce contradicciones visuales y evita conclusiones prematuras con pocos trades.

**Desventajas:** si `stats.json` esta desactualizado, Home y Estadisticas compartiran el mismo desfase hasta que Analytics lo reconstruya.

**Mejoras futuras:** mostrar freshness de `stats.json` en Home o Sistema si la edad del indice supera un umbral.

## Versionado Historico y Reparacion Auditable

**Problema:** algunos datos historicos fueron generados antes de fixes importantes de observabilidad, reconciliacion o contabilidad. Sin metadata de version es dificil decidir si un trade debe usarse, excluirse o marcarse como parcialmente confiable.

**Alternativas consideradas:** reescribir los historicos, confiar solo en commits Git, o mantener un registro explicito de capacidades/bugs por rango.

**Solucion actual:** `trading/version_history.py` define metadata read-only con rangos historicos, capacidades, bugs conocidos, limitaciones, fixes y politica de uso de datos. Tambien expone la metadata runtime (`bot_version`, `strategy_version`, `data_schema_version`) y `attach_version_metadata(...)` para enriquecer nuevos registros al persistirlos. `docs/VERSION_HISTORY.md` documenta la misma informacion para mantenimiento humano. `trading/repair_data_quality.py` existe solo como scaffold dry-run: genera un plan auditable desde `audit_data_quality.py`, soporta `--plan version-backfill`, rechaza `--write`/`--apply` y no modifica archivos historicos.

**Ventajas:** permite clasificar registros viejos sin alterar el bot ni reescribir datos. Prepara un camino seguro para reparaciones futuras con backup, checksums y reporte.

**Desventajas:** la version actual `v1.0-alpha` sigue siendo gruesa; todavia falta convertir cada fix relevante en release formal con fecha precisa.

**Mejoras futuras:** versionar cada fix operativo/observabilidad como release interno mas granular y habilitar reparaciones una por una solo con dry-run aprobado, backup, checksums y reporte.

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

**Solucion actual:** desde `v1.2-sizing-v2`, el sizing separa Spot y Futures por tipo de exposicion. Para Spot Long, `utils.get_spot_capital_per_position(...)` calcula un slot objetivo como `spot_usable * BOT_MAX_EXPOSURE_PERCENT / max_longs`, descuenta el capital ya usado por longs (`entry_price * quantity`) y limita por saldo libre con buffer. Para Futures Short, `utils.get_futures_notional_per_position(...)` calcula primero el notional objetivo como `futures_usable * BOT_MAX_EXPOSURE_PERCENT / max_shorts`, descuenta el notional abierto (`entry_price * quantity`) y deriva el margen requerido dividiendo por `FUTURES_LEVERAGE`. `capital_manager` conserva los guardrails de margen y exposicion de wallet para validar que el margen requerido entra en el capital disponible.

**Ventajas:** `BOT_MAX_EXPOSURE_PERCENT` representa mejor la exposicion real que se busca usar. Longs completos tienden a desplegar cerca del porcentaje configurado en Spot; Shorts completos tienden a usar ese porcentaje como notional objetivo, no como margen apalancado. `futures_used` puede seguir mostrando margen/initial margin sin confundirse con notional.

**Desventajas:** los datos historicos anteriores a `v1.2-sizing-v2` pertenecen al modelo anterior y no se reescriben. Si ya hubiera posiciones abiertas legacy, las nuevas entradas solo respetan el remaining exposure; no recalculan ni compensan agresivamente posiciones existentes.

**Mejoras futuras:** agregar una capa adicional de risk-to-SL para limitar perdida esperada por trade, independiente de notional y margen, y eliminar `BOT_MAX_POSITION_PERCENT` cuando no quede compatibilidad pendiente.

## Capital Ledger

**Problema:** el capital total puede cambiar por depositos, retiros, rebalanceos internos, comisiones o funding. Si esos movimientos se mezclan con PnL, un deposito manual podria parecer rendimiento de trading.

**Alternativas consideradas:** corregir PnL directamente en Analytics, inferir depositos por diferencias de balance, o crear una capa contable separada.

**Solucion actual:** `capital_ledger.py` introduce `data/history/capital_ledger.jsonl` como ledger append-only de movimientos de capital. Registra tipos explicitos como `external_deposit`, `external_withdrawal`, `rebalance`, `realized_pnl`, `commission` y `funding_fee`, con API dedicada para escribir y leer sin acoplar el resto del bot al formato JSONL. `capital_accounting.py` queda por encima del ledger y centraliza la interpretacion contable: depositos/retiros acumulados, flujos externos netos, comisiones, funding, PnL realizado y helpers preliminares de equity/PnL/ROI ajustados. `analytics_engine.py` consume esos resultados mediante `CapitalAccounting` y expone metricas adicionales sin modificar las estadisticas historicas existentes.

**Ventajas:** separa hechos contables de calculos derivados. El ledger registra movimientos; accounting interpreta esos movimientos; Analytics consume resultados contables y no lee directamente el JSONL. Las metricas actuales de Analytics se mantienen como PnL historico observado, mientras las metricas ajustadas permiten separar capital aportado por el usuario del rendimiento generado por trading.

**Desventajas:** esta primera etapa no detecta depositos automaticamente; requiere registros explicitos o integraciones futuras.

**Formulas:** `Adjusted Equity = current_equity - external_deposits + external_withdrawals`. `Adjusted PnL = Adjusted Equity - starting_equity`. `Adjusted ROI = Adjusted PnL / starting_equity * 100`. Los depositos externos se restan porque no son rendimiento; los retiros se suman de vuelta porque reducen equity actual sin representar perdida de trading. Comisiones, funding y realized PnL quedan disponibles como componentes contables separados para reportes futuros.

**Presentacion en Telegram:** `/capital` muestra una seccion de contabilidad con depositos externos, retiros, flujo neto, equity ajustado, PnL ajustado y ROI ajustado. `/stats` y `Resumen General` usan `bot_state.pnl` como fuente prioritaria para el PnL visible, igual que Home, y muestran el capital real, limite operativo y capital autorizado por separado. El limite operativo no es baseline contable ni capital invertido, por lo que no se usa para inferir perdidas de trading. El bloque `Trading ajustado` solo muestra PnL/ROI cuando existe una base inicial confiable registrada; si falta, presenta `No disponible` con el motivo. Telegram consume exclusivamente `analytics_engine`; no lee el ledger ni la capa accounting directamente.

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

**Solucion actual:** antes de recrear OCO para un huerfano Spot o una LONG en recovery sin OCO, cada flujo construye primero el payload final exacto que enviaria a Binance (`quantity`, `price`, `stopPrice`, `stopLimitPrice`) y `residuals.py` valida ese mismo payload. Binance valida la orden final, no el balance bruto: la cantidad puede bajar por `stepSize` y la pata stop-limit suele quedar por debajo del precio actual. Por eso un residual puede valer mas de 5 USDT a precio de mercado y aun asi fallar `NOTIONAL` si `payload_quantity * stopLimitPrice < minNotional`. La decision de protegible usa `min_leg_notional >= min_notional`; si cualquier pata queda bajo el minimo, no se envia POST OCO y el residual se registra como `unprotectable_residual` con motivo `oco_payload_below_min_notional`. `audit_pipeline.py`, `longs._recolocar_oco()` y `position_lifecycle.recolocar_oco_long()` usan ese helper antes de cualquier POST OCO de recuperacion. El estado guarda simbolo, asset, cantidad de balance, cantidad enviada, valor estimado, minimo requerido, payload sanitizado, precios OCO, nocional por pata, pata limitante, motivo, timestamps, contador de alertas y accion sugerida. Tambien se registra un evento `spot_residual_unprotectable` en Timeline.

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

**Seguridad en tests:** las alertas externas pasan por `notification_guard.py`. Si `BINANCEBOT_TEST_MODE=true`, `BINANCEBOT_DISABLE_EXTERNAL_NOTIFICATIONS=true` o el proceso fue lanzado como `unittest`/`discover`/`pytest`, `telegram_alerts.send_telegram_alert()` y `utils.send_alert()` suprimen Telegram y otros canales externos antes de leer credenciales o llamar transportes reales. Esto evita que fixtures con residuales, Guardian o recovery envien alertas reales durante deploy/tests aunque `.env` tenga `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`. En produccion normal, sin esos flags y sin argv de test runner, las alertas siguen usando la configuracion existente.

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

**Limpieza stale:** antes de auditar huerfanos Spot, el ciclo reconcilia `residuals_status.json` contra el balance Spot real ya leido desde Binance y contra `state.json`. Si un residual guardado tiene cantidad muy superior al balance real (`SPOT_RESIDUAL_STALE_QTY_RATIO`, default `0.5`), no tiene balance locked relevante, no fue visto recientemente (`SPOT_RESIDUAL_STALE_MIN_AGE_SECONDS`, default `3600`) y no hay posicion activa que lo justifique, se elimina del status y se registra `spot_residual_stale_cleared` en Timeline. Si el JSON esta corrupto, no se sobrescribe.

**Dust cleaner existente:** el repositorio conserva un flujo de limpieza de dust conectado al ciclo actual. `utils.clean_dust(dry_run=True)` detecta saldos no protegidos por `DUST_PROTECTED`, exige un valor total minimo (`DUST_MIN_VALUE_USD`) y usa el endpoint Binance `/sapi/v1/asset/dust`, que convierte activos pequenos a BNB. `BinanceClient.clean_dust()` delega en ese helper y `audit_pipeline.maybe_clean_dust()` lo invoca desde `CycleRunner` con frecuencia semanal controlada por `DUST_CLEAN_DAY` y estado local.

**Compuerta explicita:** `DRY_RUN` controla el trading, no la limpieza de dust. La conversion real de dust requiere `AUTO_CLEAN_DUST=True` y `DUST_CLEAN_DRY_RUN=False`. Por defecto `AUTO_CLEAN_DUST=False` y `DUST_CLEAN_DRY_RUN=True`, por lo que el ciclo puede omitir o simular la limpieza, pero no convertir automaticamente.

**Riesgos:** aunque el helper existe y es reutilizable, una conversion real depende de permisos de dust conversion en Binance, cambia balances Spot y puede afectar auditoria contable. Por eso queda desactivada por defecto y separada del modo real/simulado de trading.

**Mejora futura:** crear un `Dust Manager` pasivo por defecto. Sus responsabilidades serian detectar dust sin posicion activa ni orden abierta, verificar valor estimado y elegibilidad de conversion/venta, persistir estado, alertar, y ejecutar limpieza solo si una configuracion explicita lo habilita.

## Reconciliacion Futures Observada

**Problema:** Binance puede mantener posiciones Futures abiertas aunque el historial interno tenga un cierre total o aunque `state.json` ya no conserve una posicion gestionada. Esto consume margen, bloquea rebalance y deja riesgo operativo si no hay ordenes abiertas de proteccion.

**Decision actual:** `futures_reconciliation.py` clasifica posiciones Futures observadas desde Binance de forma pasiva. No cierra posiciones, no modifica payloads y no cambia la logica normal de entradas/salidas. Persiste `data/history/futures_reconciliation_status.json` con posicion, margen, estado de gestion interna, presencia de open orders, clasificaciones y severidad. Ese archivo es la fuente reconciliada para Home, Capital y Posiciones.

**Fuente de datos:** la reconciliacion usa posiciones observadas desde Binance, preferentemente el payload crudo de `futures_position_risk()` con `positionAmt != 0`. Si solo existe el snapshot normalizado usado por Telegram, tambien acepta `quantity` + `side`. Nunca depende solo de `state.json`.

**Clasificaciones:** `observed_futures_position` significa que Binance reporta `positionAmt != 0`. `managed_futures_position` existe tambien en `state.json` con metadata suficiente de lifecycle. `unmanaged_futures_position` y `orphan_futures_position` indican que Binance ve la posicion pero el bot no tiene lifecycle confiable. `unprotected_futures_position` indica que no hay open orders. `desynced_closed_but_open_on_exchange` indica que el historial tiene cierre total pero Binance sigue abierto. `stale_observed_futures_position` marca posiciones observadas con antiguedad estimada mayor a 24h.

**Desync por historial:** `desynced_closed_but_open_on_exchange` se evalua contra el `trade_id` gestionado cuando existe. Un cierre historico viejo del mismo simbolo no basta para marcar desync si `state.json` gestiona la posicion actual y `trades.jsonl` contiene `TRADE_OPEN status=OPEN` para ese `trade_id`. Esto evita falsos positivos en shorts sanos con TP reduce-only abierto y SL nativo vacio.

**Parsing Binance:** shorts con `notional` negativo siguen siendo posiciones abiertas si `abs(positionAmt) > 0`. Para totales y resumen se usa `abs(notional)`, preservando `position_amt` firmado para entender el lado real.

**Estado reconciliado:** `ALINEADO` solo aplica si `observed_count <= allowed_count` cuando hay limite disponible, y ademas `unmanaged_count`, `orphan_count`, `unprotected_count` y `desynced_count` son cero. Si hay exceso contra capacidad o posiciones no gestionadas, el estado explicita ambos motivos.

**Presentacion Telegram:** Home y Capital usan formato compacto cuando la reconciliacion esta sana: `Shorts: positions.short.current/positions.short.max` y `Futures: usado / real`. El bloque expandido con observadas, gestionadas, permitidas, sin proteccion y estado aparece solo si hay riesgo: exceso contra la capacidad operativa, posiciones no gestionadas/huerfanas/sin proteccion/desincronizadas o estado no alineado. `futures_reconciliation.allowed_count` es diagnostico de reconciliacion y solo se muestra en el bloque expandido de riesgo como `Permitidas ahora`; no reemplaza `positions.short.max` en UI sana. Esto es solo UI/observabilidad y no modifica trading, recovery ni rebalance.

**Hallazgo stale/24h:** la regla stale opera sobre posiciones activas en `state.json`. Si una posicion residual queda fuera de `state.json` o fue registrada como cerrada antes de confirmar `positionAmt=0`, el lifecycle normal ya no la recorre y la regla stale no puede cerrarla. Por eso esta iteracion solo alerta y clasifica.

**Riesgo:** una posicion Futures sin TP/SL/reduce-only abierta puede seguir acumulando PnL no realizado y bloquear transferencias. El bot no debe cerrarla automaticamente sin un flujo explicito de recovery porque podria cerrar una posicion que requiere revision humana o conciliacion de historial.

**Recovery manual:** `futures_recovery.py` implementa un flujo read-confirm-execute para posiciones Futures huerfanas/no gestionadas/desincronizadas. `/futures_recovery_preview` lista candidatas y la orden propuesta sin enviar nada. `/futures_recovery_close SYMBOL CONFIRM` cierra solo ese simbolo si supera pre-checks.

**Pre-checks:** el simbolo debe existir en `futures_reconciliation_status.json`, no debe estar gestionado activamente, debe tener clasificacion de recovery (`unmanaged`, `orphan`, `unprotected` o `desynced`), debe incluir confirmacion literal `CONFIRM`, se reconsulta `futures_position_risk(symbol)` antes de cerrar y se valida cantidad contra `stepSize/minQty`.

**Orden de recovery:** SHORT (`positionAmt < 0`) se cierra con `BUY MARKET reduceOnly=true`; LONG (`positionAmt > 0`) con `SELL MARKET reduceOnly=true`. `reduceOnly` es obligatorio para impedir abrir una posicion nueva por error.

**Politica preventiva de residuales gestionados:** despues de un parcial SHORT, el bot reconsulta Binance con `futures_position_risk(symbol)` y `futures_open_orders(symbol)` antes de confiar en `state.json`. Si Binance confirma `positionAmt=0`, se limpia la posicion local. Si queda una posicion real sin ordenes y `abs(notional) <= FUTURES_RESIDUAL_MAX_NOTIONAL_USDT` (default `3.0`), `futures_residuals.py` puede cerrarla con MARKET `reduceOnly=true` cuando `FUTURES_RESIDUAL_CLOSE_ENABLED=true`. El cierre genera reporte auditable en `data/history/repair_reports/` y evento de Timeline. No se recalcula PnL ni se duplica el cierre parcial ya registrado por el lifecycle normal.

**Posiciones sin proteccion no residuales:** si queda una posicion Futures real sin open orders y su notional supera el umbral residual, el bot intenta recrear proteccion reduce-only con los TP/SL ya existentes en el estado. Si no puede verificar proteccion posterior, marca `futures_entries_blocked=true`, registra evento critico y bloquea nuevas entradas SHORT con `FUTURES_UNPROTECTED_BLOCK_NEW_ENTRIES=true` (default). La gestion/cierre de posiciones existentes sigue permitida.

**Recovery explicito para residual gestionado:** `futures_recovery.close_managed_residual(symbol, confirm="CONFIRM")` es una ruta separada de `close_position()`. Solo acepta posiciones `managed_in_state=true`, observadas en Binance, sin proteccion o sin open orders, con notional dentro del umbral. Siempre usa `reduceOnly`, reconsulta Binance antes y despues, persiste reporte auditable y limpia `state.json` solo si Binance confirma `positionAmt=0`. Posiciones grandes no se cierran automaticamente.

**Riesgos restantes:** si Binance rechaza por precision, minimo, margen o estado de posicion, el recovery no reintenta ni fuerza cierre. Registra timeline con `code/msg/raw_body` y deja el caso para revision manual.

**Mejora futura:** agregar una segunda capa opcional de recovery con aprobacion persistente, reporte post-cierre y reconciliacion automatica del estado una vez confirmado `positionAmt=0`.

## Clasificacion de Fallos de Parciales

**Problema:** un cierre parcial Futures puede fallar con HTTP 400 aunque, al verificar inmediatamente contra Binance, la posicion ya haya quedado cerrada por TP/SL/trailing o por una orden previa. Alertar ese caso como riesgo operativo genera ruido porque no queda exposicion abierta.

**Decision actual:** `position_lifecycle.py` clasifica el fallo despues de consultar el estado real. Para SHORT reconsulta `futures_position_risk(symbol)` y `futures_open_orders(symbol)`; para LONG reconsulta balance Spot y open orders. Si la posicion ya no existe, el evento queda como `position_already_closed` con severidad `INFO`. Si sigue abierta con ordenes, queda como `still_open_protected`. Solo se alerta como riesgo cuando sigue abierta sin proteccion o cuando no se pudo verificar el estado real.

**Observabilidad:** cada caso registra `PARTIAL_CLOSE_FAILED` en Timeline con `resolution`, detalle HTTP de Binance, cantidad intentada, cantidad en state, `position_amt_after_check` y cantidad de open orders. Esto no cambia la logica de trading ni recalcula PnL; solo evita alertas operativas falsas cuando el exchange confirma que no hay riesgo pendiente.

## Auditoria Local de Datos

**Problema:** los historicos JSON/JSONL pueden degradarse con campos faltantes, lineas corruptas, timestamps invalidos o relaciones incompletas entre trades, features, timeline, rebalance y ledger.

**Solucion actual:** `audit_data_quality.py` es una herramienta local de solo lectura que valida archivos runtime/historicos, resume errores criticos, warnings, campos faltantes, completitud y recomendaciones. Devuelve exit code `1` si encuentra errores criticos y `0` cuando solo hay warnings o todo esta correcto.

**Separacion de warnings:** el auditor distingue `warnings operativos recientes`, `warnings legacy/historicos` y `warnings conocidos aceptados`. Los warnings operativos representan posibles problemas actuales de recoleccion o estado runtime. Los legacy/historicos agrupan registros clasificados por version antigua (`legacy-pre-history`, `v1.0-alpha`) o timestamps historicos. Los conocidos aceptados cubren backfills/imports/recoveries explicitos, donde el dato puede ser incompleto por origen pero no debe ensuciar el diagnostico operativo diario.

**Ruido no accionable:** los gaps/out-of-order de trades cerrados, features asociados a trades ya cerrados sin evidencia runtime activa, y snapshots historicos con metadata de backfill/import/synthetic se clasifican como conocidos aceptados o legacy. Un `MARKET_SNAPSHOT` con timestamp viejo escrito por una version actual sin metadata de backfill se mantiene operativo como `stale_snapshot_timestamp_generated_recently`. Registros sospechosos como `trade_id=t1` no se silencian automaticamente: requieren plan dry-run y reparacion auditada con backup antes de salir de warnings operativos.

**Reparacion auditada de scaffolds:** `repair_data_quality.py --plan suspicious-test-record --trade-id t1` inspecciona ocurrencias exactas de `trade_id=t1`. La escritura esta limitada a `--write --confirm-trade-id t1`, crea backup por archivo en `data/history/backups/`, reporte en `data/history/repair_reports/` y elimina solo lineas JSONL validas con `record.trade_id == "t1"`. No reordena archivos, no elimina `t10`/`at1`/`test_t1` y no toca snapshots sin `trade_id`.

**Snapshots stale:** `repair_data_quality.py --plan stale-market-snapshots --timestamp 2026-06-30T12:00:00Z` es solo diagnostico. Agrupa ocurrencias por `bot_version` y `event_type`, muestra contexto anterior/posterior, metadata backfill/import/synthetic y recomendacion. No escribe ni corrige snapshots en esta iteracion.

**Timestamp de `MARKET_SNAPSHOT`:** la causa real de snapshots stale fue que `AnalyticsLogger._record_history_open` y `DecisionSnapshotLogger._record_history_snapshot` pasaban timestamps de entrada/ciclo a `history.record_snapshot`, y `HistoryStore.record_snapshot` los usaba como `timestamp` principal del evento. El contrato queda corregido: snapshots normales usan `timestamp`/`recorded_at`/`generated_at` actuales y conservan el timestamp de mercado o entrada como `source_timestamp`. Solo backfills/imports/synthetic/recovered con metadata explicita pueden mantener un timestamp historico como timestamp principal.

**Limpieza auditada de snapshots stale:** `repair_data_quality.py --plan stale-market-snapshots --timestamp <ts> --write --confirm-timestamp <ts>` elimina exclusivamente lineas JSONL validas de `data/history/snapshots.jsonl` con `event_type=MARKET_SNAPSHOT`, timestamp exacto, sin `trade_id` ni `order_id`. Crea backup y reporte obligatorio; no toca JSON corrupto, trades, rebalance, ledger, residuals ni recovery.

**Trades abiertos y parciales:** un `TRADE_OPEN` sin cierre no es warning operativo si existe evidencia actual en `trading/state.json`, `trading/bot_state.json` o `data/history/futures_reconciliation_status.json` de que la posicion sigue gestionada. Se reporta como `active_open_trade` informativo. Los cierres `:partial` con `trade_id` base relacionado se clasifican como `partial_close_with_related_base` en warnings conocidos aceptados. IDs sin evidencia runtime, especialmente scaffolds como `t1`, siguen como warning operativo `suspicious_test_record`.

**Ventajas:** permite auditar calidad antes de usar los datos para Analytics avanzado, PnL ajustado o aprendizaje futuro sin tocar estrategia ni estado operativo.

## Data Quality Repair Plan

**Estado investigado:** los faltantes historicos de `features.jsonl` antes de que el Feature Store guardara `market.regime` son deuda historica y no deben rellenarse sin migracion auditable. El registro reciente `long_NEARUSDT_recovered_1783299788` corresponde al flujo de recovery Spot: se genera desde `audit_pipeline.audit_orphans()` y entra a persistencia con `candidate=None` y `btc_ctx=None`, por lo que no puede tener scoring normal ni contexto BTC completo. Para nuevos registros, `safe_log_open()` ahora persiste `capital_at_entry` observacional como `entry_price * quantity` si el flujo no entrega capital explicito, de modo que `capital.position_final` no quede vacio en recoveries futuros.

**Clasificacion auditor:** los features de recovery usan schema reducido: deben conservar `trade_id`, simbolo, direccion, wallet, `entry_price` y `capital.position_final`, pero no requieren `scoring.score_total`, `market.btc_price` ni `market.btc_change_4h`. Un registro reciente no-recovery con esos campos faltantes se clasifica como posible bug actual de recoleccion, no como dato historico esperado.

**WLDUSDT:** un cierre total como `short_WLDUSDT_1782763085` sin apertura previa no debe repararse a ciegas. La clasificacion correcta inicial es `requires_manual_review` hasta confirmar en `trades.jsonl`, `trade_analytics.jsonl`, timeline y estado si falta realmente `TRADE_OPEN`, si el trade fue importado/reconciliado o si hubo cambio de `trade_id`. `repair_data_quality.py --plan trade-gap --trade-id short_WLDUSDT_1782763085` genera un plan dry-run con evidencia por archivo, clasificacion y acciones propuestas. Si el plan confirma `open_found` en `trading/trade_analytics.jsonl`, `repair_data_quality.py --plan trade-open-backfill --trade-id short_WLDUSDT_1782763085` prepara un backfill puntual del `TRADE_OPEN` faltante en `data/history/trades.jsonl`.

**Backfill auditado:** `trade-open-backfill` es dry-run por defecto. Solo escribe con `--apply --confirm-trade-id <trade_id>`, exige un unico OPEN exacto en `trade_analytics.jsonl`, exige cierres relacionados en `data/history/trades.jsonl`, rechaza el caso si el OPEN ya existe, crea backup de `trades.jsonl`, calcula checksums antes/despues y guarda reporte JSON en `data/history/repair_reports/`. La reparacion no recalcula PnL, no modifica cierres y no toca logica operativa. La metadata de version se infiere con `version_history.classify_record(...)` usando `opened_at`/`entry_time`; no usa la version runtime actual para registros historicos. Si el OPEN fuente trae `bot_version` contradictoria con el timestamp, el registro reparado conserva la version historica inferida y deja la contradiccion en `repair_metadata`.

**Timestamps y gaps:** `trade_analytics.jsonl` puede contener entradas recuperadas/importadas con `entry_time` historico escritas en append durante una corrida posterior. Eso explica algunos out-of-order/gaps sin implicar automaticamente bug actual. El auditor no los silencia: los reporta como warnings y la revision debe distinguir import/recovery historico de escritura normal fuera de orden.

**Higiene dry-run:** `repair_data_quality.py --plan data-hygiene-backfill` revisa `trading/trade_analytics.jsonl` y propone, sin escribir, backfills simples de metadata si la fuente es verificable en el mismo registro o por `version_history`. Puede sugerir `market.regime` desde `regime`/`market_regime`, `capital.position_final` solo desde campos escalares claros (`position_final`, `capital_used`, `notional`) y `bot_version` inferible por timestamp. Los casos sin fuente confiable quedan como `optional_unresolved`; no se inventan valores ni se reescribe historial.

**Inspeccion de registros sospechosos:** `repair_data_quality.py --plan suspicious-test-record --trade-id t1` genera un reporte dry-run de evidencia para IDs que parecen basura de test/scaffold. No borra, no reordena y no habilita escritura; cualquier limpieza futura requiere plan explicito con backup y confirmacion.

**Migracion futura:** una futura herramienta `trading/repair_data_quality.py` deberia ser `--dry-run` por defecto, crear backup automatico antes de modificar, escribir reporte JSON/Markdown de cambios, reparar un solo tipo de problema por ejecucion, requerir confirmacion explicita para escribir, registrar checksums antes/despues y nunca alterar datos sin trazabilidad. Los archivos candidatos a backup obligatorio son `data/history/trades.jsonl`, `data/history/features.jsonl`, `trading/trade_analytics.jsonl`, `data/history/timeline.jsonl` y cualquier estado runtime relacionado.
