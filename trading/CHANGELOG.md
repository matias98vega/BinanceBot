# Trading Bot — Changelog & Bug Log

Registro de bugs encontrados, cambios aplicados y decisiones de diseño.

---

## 2026-05-27

### 🐛 Bug: OCO siempre fallaba cuando ATR < SL mínimo
**Síntoma:**
El bot ejecutaba la compra y luego intentaba colocar la OCO 3 veces consecutivas, fallando siempre con:
> `SL demasiado cerca: 0.62% (mínimo 1.0%)`
Terminaba haciendo MARKET SELL de emergencia, perdiendo capital por fees/spread en cada ciclo.

**Causa raíz:**
El ATR de ETH en ese momento era ~0.62% del precio, pero `SL_MIN_DIST_PCT = 1.0%`. El bot compraba primero y recién después intentaba la OCO, descubriendo que el SL era inválido.

**Fix:**
1. Pre-flight antes de comprar: si `ATR < SL_MIN_DIST_PCT + 0.05% (buffer)`, fuerza el SL al mínimo y escala el TP para mantener R/R 1:2.
2. Re-validación post-compra: vuelve a verificar y ajustar con el precio real de ejecución.

---

### 🐛 Bug: Crash por error de red transitorio (DNS)
**Síntoma:**
```
socket.gaierror: [Errno -3] Temporary failure in name resolution
```
El bot crasheaba completamente ante un fallo DNS momentáneo.

**Causa raíz:**
`urlopen()` sin manejo de errores de red.

**Fix:**
Nueva función `_urlopen_with_retry()` con 3 reintentos y backoff exponencial (2s, 4s). Todas las llamadas HTTP pasan por ahí.

---

### ⚙️ Mejora: Filtros de contexto global ampliados (3 nuevos)
**Implementados en `market_context_ok`:**

1. **Filtro horario:** no entrar entre 22:00-06:00 UTC (baja liquidez, spreads amplios).
2. **Volatilidad extrema BTC:** si ATR 4h de BTC supera 4% → mercado en pánico, no entrar.
3. **Tendencia macro BTC (EMA50 vs EMA200 diario):** si EMA50 < EMA200 en 1D → tendencia bajista estructural, no entrar. Detectado en producción: BTC EMA50 $76,685 < EMA200 $81,382.

Parámetros: `BTC_BEAR_TREND=True`, `BTC_ATR4H_MAX_PCT=4.0`, `NO_ENTRY_HOURS=(22,6)`

---

### 🐛 Bug: OCO huérfana de NEAR bloqueaba $8 de capital
**Síntoma:** USDT libre = $0.78 aunque el capital real era ~$9. NEAR tenía 3.1 unidades locked en una OCO no registrada en state.json.

**Causa raíz:** Un trade de NEAR cerrado como STALE dejó la OCO activa en Binance sin cancelarla correctamente. El state.json se actualizó a `scanning` pero la OCO siguió vigente.

**Fix manual:** Se canceló la OCO (orderListId 22401249637) y se vendió el NEAR en mercado. Capital recuperado: $9.0354.

**Pendiente:** ~~Agregar chequeo de órdenes huérfanas al inicio del ciclo.~~ ✅ Implementado.

---

### ⚙️ Mejora: ATR mínimo subido de 0.5% → 1.0%
**Motivo:**
`ATR_MIN_PCT = 0.5%` pero `SL_MIN_DIST_PCT = 1.0%`. Un par con ATR entre ambos valores pasaba el filtro de análisis y fallaba después. Ahora se descartan antes de cualquier compra.

---

### 🐛 Bug: SL 0.98% a pesar del pre-flight que calculó 1.0% (tick rounding)
**Síntoma:**
OCO seguía fallando con `SL demasiado cerca: 0.98%` aunque el pre-flight calculó 1.0%.

**Causa raíz:**
`round_price()` con el tick de DOGE redondeaba el SL hacia arriba, achicando la distancia real.

**Fix:**
Se fuerza `SL_MIN_DIST_PCT + 0.05 = 1.05%` como distancia objetivo antes del redondeo de tick, garantizando que el resultado final siempre supere el 1.0% requerido.

---

### 🐛 Bug: Error -1021 "Timestamp outside recvWindow"
**Síntoma:**
```
HTTP 400: {"code":-1021,"msg":"Timestamp for this request is outside of the recvWindow."}
```

**Causa raíz:**
`signed_request()` usaba `time.time()` del sistema local, que puede estar desincronizado con Binance.

**Fix:**
Consulta `/api/v3/time` antes de firmar y usa el `serverTime` de Binance. Además agrega `recvWindow=10000` para tolerar hasta 10s de latencia de red.

---

### 🐛 Bug: "SL limit >= SL stop" en OCO (floating point + tick size)
**Síntoma:**
```
[OCO attempt 1] SL limit 1.02 >= SL stop 1.02
```
Los 3 intentos fallaban → MARKET SELL de emergencia.

**Causa raíz:**
`sl_limit = round_price(sl - tick, tick)` usaba `sl` sin redondear. Con precios como $1.02 y tick $0.001, el floating point hacía que el resultado redondeado fuera igual a `sl_r`.

**Fix:**
`sl_limit` se calcula desde `sl_r` (ya redondeado) restando 2 ticks. Con doble fallback por seguridad.

---

### 🐛 Bug: -2010 "Insufficient balance" en OCO tras emergency sells previos
**Síntoma:**
```
HTTP 400: {"code":-2010,"msg":"Account has insufficient balance for requested action."}
```
El bot compraba FIL pero la OCO fallaba los 3 intentos → MARKET SELL de emergencia.

**Causa raíz:**
Los emergency sells anteriores dejaban fracciones residuales del asset en la cuenta. La OCO usaba el `qty` calculado de la compra actual, pero el balance real del asset era mayor (compra nueva + fracciones), haciendo que el qty pedido superara el balance disponible para venta.

**Fix:**
Antes de colocar la OCO, se consulta el balance real del asset con `get_asset_balance()` y se usa ese valor (redondeado al step size) como cantidad de la OCO. El `qty` de la compra solo se usa como fallback si el balance real es menor al mínimo.

---

### ⚙️ Mejora: Filtro de correlación BTC y spread bid/ask

**Correlación BTC:**
Si BTC bajó >0.5% en 4h Y el par tiene correlación de Pearson >0.85 con BTC en 1h → descartado.
Razonamiento: un par muy correlacionado con BTC en caída probablemente lo siga hacia abajo aunque su score individual parezca bueno.
Solo se calcula la correlación cuando BTC está débil, para no agregar latencia innecesaria.

**Spread bid/ask:**
Antes de agregar un par a los candidatos, se consulta `/api/v3/ticker/bookTicker`.
Si el spread > 0.3% del precio → descartado. Evita entrar en pares con liquidez baja donde el costo de entrada ya es caro antes de considerar fees.

Parámetros: `BTC_CORR_MAX=0.85`, `BTC_WEAK_PCT=-0.5`, `SPREAD_MAX_PCT=0.3`

---

### ⚙️ Mejora: Trailing stop revisado y reforzado
**Cambios:**
1. **Buffer de 0.1%** en el SL del step 1: antes llevaba el SL a breakeven exacto, que con fees resultaba en pérdida. Ahora step 1 asegura +0.1% (cubre fees del round-trip).
2. **Alerta Jarvis** cuando el trailing se activa: muestra par, SL viejo → nuevo, precio actual y ganancia mínima asegurada.
3. **Alerta urgente** si el OCO no se puede restaurar tras fallo del trailing: antes quedaba silencioso sin stops.

**Tabla de steps con entrada $83.61:**
- Sube 1%: SL → $83.69 (+0.1% asegurado)
- Sube 2%: SL → $84.53 (+1.1% asegurado)
- Sube 3%: SL → $85.37 (+2.1% asegurado)
- Sube 5%: SL → $87.04 (+4.1% asegurado)

---

### ⚙️ Mejora: Notificaciones de contexto bajista de mercado
**Motivo:**
El bot podía estar horas sin entrar por mercado bajista sin que el usuario supiera el motivo.

**Implementación:**
Cuando `analyze_market()` devuelve contexto bajista, se envía alerta via Jarvis con el motivo exacto (BTC tendencia, amplitud de mercado, etc.).
Throttle: notifica solo cuando cambia el motivo o pasaron >4h desde la última alerta — evita spam cada 30 min con el mismo mensaje.

---

### 🐛 Bug: Fórmula de PnL frágil (`usdt_now - (capital - total_pnl)`)
**Síntoma:**
El `total_pnl_usdt` se corrompía acumulando errores — llegó a mostrar -$5.66 cuando la pérdida real era -$0.97. Requirió correcciones manuales múltiples veces en el día.

**Causa raíz:**
La fórmula `pnl = usdt_now - (capital_usdt - total_pnl_usdt)` es circular y frágil: cualquier inconsistencia en `capital_usdt` o `total_pnl_usdt` se propaga y amplifica en cada ciclo.

**Fix:**
Nueva función `calc_trade_pnl(entry_price, exit_price, qty)` que calcula directamente:
`PnL = (salida - entrada) × qty - fees`
Para trades cerrados por OCO (TP/SL), se obtiene el precio real de ejecución desde `/api/v3/myTrades`. Eliminada la fórmula frágil en todos los 6 lugares donde aparecía.

---

### ⚙️ Mejora: Sensor de contexto global de mercado
**Motivo:**
El bot podía entrar en trades durante caídas generalizadas de mercado, ya que solo chequeaba cada par individualmente (caída >5% en 24h), sin ver el panorama global.

**Implementación (`market_context_ok`):**
1. **BTC 4h:** si BTC cae más de 2.5% en las últimas 4h → mercado bajista, no entrar.
2. **Amplitud:** si >60% de los pares del watchlist están en rojo en 24h → no entrar.

Cualquiera de los dos bloquea el análisis completo. Se loguea el motivo en `analysis_log.txt`.

Parámetros: `BTC_DROP_4H_PCT = 2.5`, `MARKET_RED_PCT = 60.0`

---

### 🐛 Bug: Fallback ignoraba score mínimo — SOL entró con score 3/8
**Síntoma:**
SOL fue elegido con score 3 (mínimo requerido: 5) y RSI 29.

**Causa raíz:**
Cuando ningún par pasaba los filtros, un bloque fallback rescataba ETHUSDT/SOLUSDT/BNBUSDT saltándose la validación de score mínimo. Solo chequeaba RSI y ATR.

**Fix:**
Se eliminó el fallback. Si no hay candidatos que cumplan todos los filtros, el bot espera al próximo ciclo de 30 min. Mejor no operar que operar mal.

---

## Ideas / Backlog

### 💡 Chequeo de órdenes huérfanas al inicio del ciclo
Al arrancar cada ciclo, verificar si hay OCOs/órdenes abiertas en Binance que no estén registradas en state.json. Si se encuentran, cancelarlas y liberar el capital. Evita que el bot opere con capital inmovilizado sin saberlo.

### 💡 Modo dual: Long (Spot) + Short (Futuros)
Cuando el mercado está alcista → operar long en Spot (modo actual).
Cuando el mercado está bajista (EMA50 < EMA200, amplitud >60% rojo) → operar short en Futuros USDⓈ-M.

**Requiere:**
- Migrar la lógica de entrada/salida a Futuros (`/fapi/v1/`)
- Scoring invertido para shorts (RSI alto = sobrecomprado = entrada short)
- Control de leverage (recomendado x1-x2 para empezar)
- Monitoreo de funding rate y liquidation price
- Modo dual en state.json: `mode: spot | futures`

**Prerequisito:** bot Spot estable y capital suficiente (>$50 recomendado para Futuros).


Cuando el bot está `in_position` y en ganancia, evaluar salida antes de que toque TP si:
- RSI supera 80 (sobrecomprado extremo)
- MACD histograma colapsa (divergencia bajista)

Requiere backtesting antes de activar. **Pendiente de validación.**

---

## Convenciones

- **🐛 Bug** — error que causó comportamiento incorrecto o pérdida de capital
- **⚙️ Mejora** — ajuste de parámetro o lógica sin bug explícito
- **🔒 Seguridad** — cambio relacionado con protección de capital o manejo de errores críticos
- **📊 Análisis** — cambio en lógica de scoring o selección de pares
