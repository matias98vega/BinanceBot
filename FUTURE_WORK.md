# Future work

## Odd-step Futures partial split — COMPLETED in v1.3

The AMDUSDT `0.03` incident demonstrated that independently rounding both halves down produced `0.01 + 0.01`, leaving an unmanaged `0.01` residual.

The deployed behavioral change now:

- use `Decimal`;
- normalize the partial quantity to `stepSize`;
- calculate `remaining = initial - partial`;
- prove `partial + remaining == initial`;
- cover odd quantities, minQty/minNotional, rejected/partial fills and recovery;
- preserve historical records and require its own review because it changes position management.
