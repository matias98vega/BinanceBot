# Partial quantity integrity

Runtime `v1.3-partial-quantity-fix` introduces behavioral capability
`partial-quantity-integrity-v1`.

The AMDUSDT incident exposed the former SHORT formula:

```text
partial = round_down(initial * 0.5, stepSize)
remaining = round_down(initial * 0.5, stepSize)
```

For `0.03` and `stepSize=0.01`, that produced `0.01 + 0.01` and lost one
step locally. The corrected invariant is:

```text
normalized_initial = normalize_down(initial, stepSize)
partial_request = normalize_down(normalized_initial * fraction, stepSize)
remaining = normalized_initial - confirmed_executedQty
partial_executed + remaining = normalized_initial
```

All external numeric values enter Decimal through strings. A zero normalized
partial is skipped. `executedQty` from the confirmed order is authoritative;
PARTIALLY_FILLED uses only its confirmed amount. NEW, canceled, rejected,
timeout or unknown results do not reduce local quantity. An executed amount
above the request or not aligned to stepSize is inconsistent.

After a confirmed fill, the existing position query verifies
`abs(positionAmt)`. A mismatch leaves the original local quantity in place,
records `POSITION_MISMATCH`, and requires reconciliation. Only an aligned
remainder replaces TP/SL quantities; price levels and Guardian behavior are
unchanged.

Spot LONG is not affected by the AMD split bug: its remaining OCO quantity is
derived from the observed free balance minus the sale quantity, rather than a
second independent half used as managed state.

This is future-only management behavior. Existing trades retain their opening
`bot_version`; closes and partials recover that canonical version. There is no
backfill. `short_AMDUSDT_1784906790` and its separately accounted cleanup remain
immutable. The pre-entry gate remains `AUDIT_ONLY`.
