# Fake Binance Client

`trading/testing/` contiene un harness reutilizable, determinista y exclusivamente de tests. No importa `utils`, no lee credenciales, no abre sockets y no puede activarse por configuración de runtime. Los tests deben inyectar una instancia de `FakeBinanceClient` explícitamente o parchear la referencia local de un módulo.

## Arquitectura

- `FakeExchangeState`: fuente de verdad in-memory para balances Spot free/locked, wallet y posiciones Futures, precios, klines, filtros, órdenes, OCO, trades, transferencias, leverage, reloj lógico, IDs, errores en cola y call log.
- `FakeBinanceClient`: implementa los helpers consumidos del `BinanceClient` y los endpoints raw que usa el bot. Los endpoints desconocidos fallan con `NotImplementedError`; nunca hacen fallback al cliente real.
- `scenarios.py`: escenarios deterministas A–L para long, OCO, TP/SL, stale Spot, short, reduceOnly, orphan, rebalance simulado, circuit breaker y capacidad.

## Uso

```python
from testing import FakeBinanceClient, FakeExchangeState

state = FakeExchangeState()
state.set_balance('USDT', 100)
state.set_price('BTCUSDT', 100)
client = FakeBinanceClient(state)
client.create_spot_order({
    'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET', 'quantity': '.1',
})
assert client.assert_called('spot_signed:POST:/api/v3/order')
```

También se puede usar `build_scenario('A')` o `all_scenarios()`. Cada construcción crea estado nuevo: no se comparte estado entre tests. `state.advance(seconds)` avanza el reloj sin sleeps y `state.queue_error(operation, exc)` programa fallos.

## Métodos soportados

Cuenta/balances y precios; klines; filtros/exchange info; órdenes market Spot; consulta/cancelación de órdenes; OCO, TP/SL y fill parcial controlado; órdenes Futures market/limit/stop; leverage; posiciones/uPnL; open orders; `reduceOnly`; myTrades; transferencias internas simuladas; snapshot y registro de llamadas. Se soportan tanto helpers como los paths raw actualmente consumidos por producción.

## Semántica y diferencias respecto de Binance

Los market orders llenan inmediatamente al precio configurado. En Spot, BUY cobra la fee sobre el activo base y SELL sobre USDT. Futures cobra fee sobre notional y realiza PnL al reducir exposición. `TRADING_FEE` del ledger no se genera aquí: el harness modela exchange, no contabilidad persistente. OCO reserva saldo y permite disparar TP/SL de forma explícita. `reduceOnly` nunca aumenta exposición.

No se modelan matching engine, latencia, slippage, rate limits, firma, liquidación, funding programado, margin modes completos ni todos los endpoints de Binance. `clean_dust(dry_run=False)` y endpoints no inventariados fallan explícitamente. Estas diferencias impiden tratarlo como emulador fiel o cliente productivo.

## Garantía no-network y aislamiento

El paquete no importa el transporte productivo. Los tests parchean `socket.socket` y `socket.getaddrinfo` para convertir cualquier intento de red/DNS en un fallo. Los escenarios no aceptan rutas productivas; las pruebas con archivos usan `TemporaryDirectory`. No existe selector por variable de entorno ni cambio en `binance_client.py`, servicios, estrategia o payloads reales.

Los mocks locales existentes permanecen cuando prueban una secuencia muy estrecha o errores HTTP concretos. Se pueden migrar gradualmente cuando el fake aporte una semántica mejor sin ocultar el propósito del test.

## Limitaciones y siguiente fase

El siguiente paso posible es un `ReplayClient` que reproduzca respuestas sanitizadas y versionadas. No está implementado ni se considera completado.
