# Paused zero-exposure preflight

The operator deploy can classify fresh zero exposure while the current runtime
is paused by `daily_stop_loss_limit`. Such cycles return before Futures
reconciliation and persist an empty `{}` summary in `bot_state.json`.

This route requires all of the following:

- The local position list is exactly empty, and the existing pause has a known
  reason, today's UTC PnL date and valid risk counters. Both runtime early-return
  branches are recognized: `status=paused`, or `status=active` with a strictly
  future `pause_until` (the UTC daily reset leaves the timed breaker intact).
  An active bot with an expired, missing or invalid expiry is not eligible.
- The local reconciliation field exists and is exactly `{}`. An existing
  contradictory summary is never replaced.
- Complete authenticated GET responses show zero Futures exposure and no
  Futures or Spot open orders. Spot account evidence also passes normal checks.
- Both local state files are at most 300 seconds old and remain byte-identical
  throughout the observation. All reads finish within 30 seconds without a UTC
  day change.

The resulting in-memory classification reports
`FUTURES_RECONCILIATION_SOURCE=FRESH_GET_PAUSED_ZERO_EXPOSURE` explicitly.
It never writes a fabricated summary to runtime or history. Existing Spot
compatibility and all normal safety checks remain in force.

After isolated preparation and the first preflight, the operator script stops
timers, waits for running cycles and stops resident services as before. It then
collects fresh GET evidence again before switching `current`. When the first
preflight used this route, the second must preserve the exact pause fields and
daily counters. Failure enters the existing rollback path and restores services
on the previous release.

All three successful natural cycles are checked after activation. Runtime
version and `AUDIT_ONLY` remain mandatory. Normal cycles require an explicitly
aligned persisted reconciliation; their managed exposure is not reclassified as
zero. A paused cycle with exactly `{}` instead requires the same fresh GET-only
zero-exposure observation and unchanged cutover pause fingerprint. Unknown,
stale or contradictory evidence, wrong version/mode, pause changes or exposure
on that fallback route fail verification and enter rollback. No manual cycle
is run and no reconciliation is written to state or history.

Mutable state links are reused. Deployment does not clear the pause, daily PnL
or consecutive SL counters. The runtime's existing UTC daily-reset rules still
apply during subsequent natural cycles; this preflight does not extend a pause.

There is no force option. Missing, stale, ambiguous or changing evidence blocks
activation. External exchange activity can still occur after any snapshot;
repeat the final observation immediately before activation and retain rollback.

Offline verification:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python ops/test_deploy_paused_preflight.py
PYTHONDONTWRITEBYTECODE=1 bash ops/test_deploy_immutable_release.sh
```
