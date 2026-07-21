# Functional versioning and capability epochs

BinanceBot separates trading behavior from schemas, tooling and observability. The current runtime remains `v1.2-sizing-v2`; this policy does not authorize a new runtime label.

## Taxonomy

| Field | Meaning | Changes trading by itself? |
|---|---|---|
| `bot_version` | Effective entry, exit, risk, sizing and execution behavior | Yes |
| `strategy_version` | Named rule set; future epochs should replace vague values such as `current` | Potentially |
| `feature_schema_version` | Shape and semantics of passively captured features | No |
| `data_schema_version` | Persisted record format | No |
| `model_version` | Identity of an offline, shadow or live model | Only when it affects decisions |
| `capability_epoch` | A deployed capability independent of the bot label | Not necessarily |
| `release_version` | Human-facing group of commits and capabilities | No |

Fields must never be reused with a second meaning. The declarative registry is `trading/capability_history.py`.

## Behavioral boundary

`BEHAVIORAL_VERSION_CHANGE` requires a new `bot_version`: changes to signals, scoring, entry/exit filters, sizing, exposure, leverage, operational position limits, TP/SL, trailing, circuit breakers, Guardian close policy, decision-affecting reconciliation, execution assumptions, any rule that permits/blocks trades, or a model used in live decisions.

`NON_BEHAVIORAL_CAPABILITY_CHANGE` does not: UI, docs, logs, tests, auditors, Fake/Replay, passive observability, accounting ledger, presentation fixes, passive feature capture, offline analysis and behavior-preserving refactors.

## Trade ownership

- Canonical membership is always `bot_version` on `TRADE_OPEN`.
- A close never relabels the trade; a `:partial` inherits its base opening.
- Recovered records preserve opening evidence. Without it they are `legacy/unknown`; never infer the current runtime version.
- Future safe opening contracts may add `active_capability_epochs`, `feature_schema_version`, `pre_entry_gate_mode` and `model_version`. This task adds no writes.

Two immutable historical conflicts are allowlisted and remain warnings: `short_EWYUSDT_1783476970` and `short_SAMSUNGUSDT_1783477221`. Both opened as `v1.0-alpha`; partial/close events say `v1.1-observability-hardening`; their classification is `KNOWN_IMMUTABLE_HISTORICAL_VERSION_CONFLICT` and analytics membership remains the opening version. No backfill is permitted. Any other conflict is an error in strict validation.

## Gate and ML epochs

`preentry-audit-v1` is non-behavioral while `AUDIT_ONLY`. Moving to `ENFORCE` can block entries and therefore requires an approved future `bot_version` (recommended descriptive candidate: `v1.3-preentry-enforce`, not activated). Preconditions: at least 24 hours of observation, false-block review, tuned tolerances, validated freshness/protection/capacity, full tests, feature flag, rollback and changelog evidence.

`feature-capture-v2` is passive and independent. A read-only `model-shadow-v1` may use its own `model_version` without changing `bot_version`. A live `model-filter-v1` or `adaptive-sizing-v1` is behavioral and requires a new bot version, explicit rollout and compatibility tests.

## Statistical comparison

Compare by opening version and report period, sample size, win rate, PnL, expectancy, profit factor, drawdown, side, regime, capital and exposure. Do not compare raw PnL without sizing context, mix market periods silently, or claim causality. Report confidence intervals. A segment is insufficient when class/side/regime coverage is absent, temporal validation is not viable, or uncertainty is too broad for the decision; the dataset auditor and walk-forward tools provide the operative evidence.

## Releases

- v1.0: Core Trading Engine.
- v1.1: Observability Hardening.
- v1.2: Sizing v2.
- v1.2.x: reliability, accounting, replay and offline evaluation capabilities.
- future v1.3: reserved for an approved behavioral change, not documentation or passive tooling.

Validate read-only with `python trading/check_version_consistency.py [--json|--explain|--strict]`.
