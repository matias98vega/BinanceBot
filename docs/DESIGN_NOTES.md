# Design Notes

Este documento registra decisiones de diseno importantes. Su objetivo es preservar contexto: que problema se resolvio, que alternativas existian y que costo se acepto.

## Arquitectura Modular

**Problema:** el bot necesita operar Spot, Futures, observabilidad, Telegram y dashboard sin mezclar responsabilidades.

**Alternativas consideradas:** un script unico, watchers separados por simbolo, o modulos especializados.

**Solucion actual:** `bot.py` orquesta; `market.py` decide candidatos; `longs.py` y `shorts.py` ejecutan/gestionan posiciones; `sl_guardian.py` protege; `utils.py` centraliza Binance/estado/helpers.

**Ventajas:** facilita auditar entradas, salidas y guardrails por separado.

**Desventajas:** `bot.py` aun concentra demasiada logica de ciclo y cierre.

**Mejoras futuras:** extraer un motor de ciclo y un servicio de reconciliacion state-vs-exchange.

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

**Solucion actual:** `rebalance.py` calcula targets segun contexto bullish/bearish/neutral y mueve USDT entre wallets respetando posiciones abiertas, minimo de transferencia y reserva opcional.

**Ventajas:** separa asignacion de capital de senales de entrada.

**Desventajas:** depende de balances correctos y de que las transferencias Binance respondan sin retrasos.

**Mejoras futuras:** auditoria post-transfer y simulacion dry-run de rebalance.

## Capital Manager

**Problema:** sizing y validacion de capital pueden divergir si cada modulo calcula maximos por su cuenta.

**Alternativas:** validaciones locales por modulo, constantes fijas por trade, o helper compartido.

**Solucion actual:** `capital_manager.max_margin_per_position(...)` es la fuente comun para margen maximo por operacion: capital usable por `BOT_MAX_EXPOSURE_PERCENT / max_positions`.

**Ventajas:** evita contradicciones entre sizing y guardrail.

**Desventajas:** aun existen variables deprecated de capital que deben retirarse con cuidado.

**Mejoras futuras:** eliminar `BOT_MAX_POSITION_PERCENT` cuando no quede compatibilidad pendiente.

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

## Long Spot OCO y Recovery

**Problema:** despues de comprar Spot, fees/redondeos/filtros pueden dejar menos balance libre que la cantidad teorica. Si OCO o emergency sell usan la cantidad teorica, Binance puede rechazar con `-2010`.

**Alternativas:** confiar en `executedQty`, descontar fee estimado, o consultar balance real.

**Solucion actual:** despues del BUY se consulta balance real del asset, se ajusta por stepSize/minQty/minNotional y se usa esa cantidad para OCO, retry OCO, emergency sell y recovery.

**Ventajas:** evita intentar vender/proteger mas asset del disponible.

**Desventajas:** agrega llamadas privadas adicionales y depende de que el balance se actualice rapido.

**Mejoras futuras:** polling corto de balance post-fill y mas detalle de recovery en timeline.

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
