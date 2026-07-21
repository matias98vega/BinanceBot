# Feature Registry — pre-entry context v2

The code source of truth is `trading/feature_registry.py`. Schema v2 is passive,
optional and forward-only. Historical schema v1 records remain valid and are
never rewritten.

`feature_schema_version=2` and
`feature_capture_version=preentry-context-v2` identify snapshots constructed
during candidate scoring, before order submission. Persistence may occur after
a successful fill, but retains the original capture timestamp, candle/source
timestamps, missing fields and quality flags.

Captured from already-loaded 1h klines, candidate risk and BTC context:

- EMA slopes/spreads and deterministic trend alignment;
- 1/3/6/12-candle returns, RSI and MACD histogram deltas;
- ATR expansion, realized volatility and candle anatomy;
- volume ratios/trend and quote volume;
- recent high/low distance, range position and HH/LL counts;
- relative returns against BTC;
- expected TP/SL, reward/risk and ATR-normalized distances;
- concurrent, same-side and opposite-side position counts.

No extra Binance endpoint is called. Spread/slippage, rolling BTC correlation,
beta and portfolio correlation remain future candidates because the existing
observation cannot support them reliably.

Models are not rerun automatically. The initial gate requires 150 schema-v2
closed trades (preferably 200), 50 LONG, 50 SHORT, 30 per evaluated regime,
four weeks, three temporal blocks, at least two market conditions, 90% critical
coverage and no critical leakage.
