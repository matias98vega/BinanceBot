# Instrucciones para `trading/`

## Autorización explícita

Antes de modificar cualquiera de estos elementos, obtené autorización explícita:

- estrategia, scoring, sizing o exposición;
- TP, SL, Guardian o circuit breakers;
- `BinanceClient`;
- payloads o parámetros enviados a Binance;
- órdenes, transferencias o rebalance;
- temporizadores o scheduling;
- compatibilidad de datos históricos;
- semántica de versiones del bot.

## Pruebas y aislamiento

- Probá interacciones de exchange exclusivamente con Fake, Replay o equivalentes offline.
- Ninguna prueba puede caer silenciosamente en Binance real.
- Ninguna prueba debe requerir credenciales productivas.
- Ninguna prueba debe usar red cuando el escenario pueda resolverse offline.
- Preservá los archivos runtime e históricos; no los uses como fixtures mutables.
- Usá el intérprete de `.venv` para comandos Python.

## Revisión de cambios

Ante cambios dentro de `trading/`, revisá:

- observabilidad y fuentes de verdad;
- compatibilidad histórica;
- efectos sobre capital.

Mantené los procedimientos extensos de tests y despliegue fuera de este archivo.
