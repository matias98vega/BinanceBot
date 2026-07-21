# ML Dataset Audit

Versionado: el schema pasivo de features es independiente de `bot_version`. Un shadow read-only puede tener `model_version`; un modelo que filtre trades live requiere una nueva `bot_version`. Los joins usan siempre la versión de apertura.

> Audit schema: `1`
> Reference commit before implementation: `4068eca`
> Runtime version: `v1.2-sizing-v2`
> Production snapshot fingerprint: `33b213761104d33548a58c00eb319d578560e8d3a1faff62992f7774cf02c862`

## Purpose

`python3 trading/audit_ml_dataset.py` performs a formal, offline and read-only audit. It does not train models, backfill records, infer missing values, modify historical files or participate in live decisions.

One logical ML sample is one base trade opening identified by `trade_id`. Events with the suffix `:partial` are linked to the base trade and never become independent samples.

## Current data flow

1. A successful opening is persisted in `data/history/trades.jsonl` and `trading/trade_analytics.jsonl`.
2. `feature_store.py` captures a feature snapshot associated with the opening `trade_id`.
3. A later close supplies labels in the history and analytics sources.
4. Partials remain related events under the base ID.
5. Recovered records retain explicit reduced semantics and are not silently promoted.
6. The opening event owns `bot_version`, side, symbol, regime and opening timestamp even if the close happens under later code.
7. Timeline, decisions and market snapshots are context/audit sources; they are not automatically joined as ML inputs because their temporal availability would need a separate verified join contract.
8. Capital ledger, rebalance, circuit breaker and current `bot_state` are excluded from sample construction.

## Source hierarchy

- Primary opening and closing source: `data/history/trades.jsonl`.
- Secondary equivalence/conflict source: `trading/trade_analytics.jsonl`.
- Opening feature source: `data/history/features.jsonl`.
- Context-only sources: decisions, market snapshots, decision snapshots and timeline.
- Explicitly excluded: current runtime state, exchange observations, reconciliations, ledger and system events.

Cross-source duplicates are benign only when their timestamps, PnL and normalized exit semantics agree. Conflicting closes remain excluded. No source file is rewritten or deduplicated.

## Manifest classifications

### TRUSTED

- unique identifiable opening;
- reliable later close and PnL label;
- feature snapshot at or before opening;
- minimum features present and valid;
- coherent chronology;
- opening version preserved;
- no critical duplicate or confirmed leakage flag;
- not an ambiguous recovered sample.

### PARTIAL

Useful only for explicitly listed analyses, never automatically included in the primary ML dataset. Typical reasons include an open/unlabelled trade, reduced recovered semantics, missing feature snapshot, missing minimum features or a timestamp whose availability cannot be proven.

### EXCLUDED

Missing reliable opening, conflicting or multiple primary closes, impossible chronology, post-entry feature, label embedded in features, unreliable PnL, contradictory sources or other critical contamination.

Every manifest row contains explicit `reasons`, `valid_for`, source files, missing/invalid features, duplicate flags, leakage flags and chronology flags. `quality_score` is intentionally not used.

## Current production snapshot

| Metric | Value |
|---|---:|
| Base trades | 235 |
| Closed | 232 |
| Open | 3 |
| TRUSTED | 198 |
| PARTIAL | 35 |
| EXCLUDED | 2 |
| Trusted closed | 198 |
| Wins / losses | 95 / 103 |
| Positive class | 47.9798% |
| Majority baseline | 52.0202% |
| Recovered | 3 |
| Trades with partial events | 77 |
| Legacy/v1.0 | 127 |
| Unknown regime | 2 |

The principal quality conditions are 32 missing opening feature snapshots, 30 samples missing minimum features, three open trades without closed labels, three recovered/reconstructed records and two conflicting close sources. These counts overlap because one trade may have several reasons.

`dataset_ready_for_baseline=true`: the 198 trusted closed samples support the next reproducible descriptive/statistical baseline.

`dataset_ready_for_ml=false`, blocked by:

- `VERSION_AWARE_EVALUATION_REQUIRED`;
- `DEPENDENT_SAMPLE_GROUPING_REQUIRED`;
- `FEATURE_STABILITY_NOT_YET_VALIDATED`.

## Labels

| Label | Coverage among closed | Reliability | Use |
|---|---:|---|---|
| `binary_win` | 100% | High; `1` iff closed PnL > 0 | Baseline/ML candidate |
| `pnl_usdt` | 100% | High from close; never inferred from equity | Baseline diagnostic |
| `pnl_pct` | 100% | Conditional on historical calculation semantics | Conditional |
| `return_on_capital` | 98.7069% | Conditional on reliable opening capital | Conditional |
| `R multiple` | 0% | Initial real risk unavailable in audited feature schema | Blocked |
| `exit_reason` | 100% | High after normalization | Diagnostic/auxiliary label |
| `duration_seconds` | 100% | Derived only from opening and later close | Diagnostic/auxiliary label |

Open trades are never labelled as losses. Partial events do not replace the final base-trade label.

## Feature coverage highlights

| Feature | Missing | Recommendation |
|---|---:|---|
| ATR | 0% | INCLUDE |
| RSI | 0.8511% | INCLUDE |
| Score | 0.8511% | INCLUDE |
| EMA20 / EMA50 | 12.766% | OPTIONAL pending version-aware analysis |
| Volume ratio | 12.766% | OPTIONAL |
| BTC price/change 1h/change 4h | 12.766% | OPTIONAL |
| Capital | 1.2766% | OPTIONAL/conditional |
| Notional, leverage | 100% | BLOCKED in current feature schema |
| SL%, TP% | 100% | BLOCKED; therefore R multiple is unavailable |

The JSON report includes type, valid/missing counts, non-finite/out-of-range values, cardinality, distribution summaries, constant detection and coverage by version, side and normalized regime. No imputation is performed.

## Leakage policy

Confirmed flags include `FEATURE_AFTER_ENTRY` and `LABEL_IN_FEATURES`. Risk flags include `UNKNOWN_FEATURE_TIMESTAMP` and post-trade reconstruction. Future aggregate statistics, current `bot_state`, closing prices/reasons, final PnL and final duration must never be inputs.

The current snapshot has zero confirmed flags under implemented checks. This does **not** prove absence of leakage. A future export must preserve feature lineage and verify actual availability before entry. Analytics aggregated with the same or future trades are excluded as model features.

## Temporal independence and split

Samples must be ordered by `opening_timestamp`; random-only splits are prohibited. The proposed initial split is 60% train, 20% validation and 20% final test, followed by grouped walk-forward. Boundaries must:

- keep base and partial events together;
- avoid future observations in training;
- report version, side and regime mix in every block;
- group or embargo simultaneous/nearby trades and same-cycle conditions;
- consider day/week clusters and repeated symbols;
- fail with `TEMPORAL_SPLIT_INSUFFICIENT_SAMPLE` when blocks are not viable.

The current split is numerically possible but carries `VERSION_MIXING_RISK` and `DEPENDENT_SAMPLES_RISK`. The baseline task must compare version-aware and grouped temporal variants.

## CLI

```bash
python3 trading/audit_ml_dataset.py
python3 trading/audit_ml_dataset.py --json
python3 trading/audit_ml_dataset.py --explain
python3 trading/audit_ml_dataset.py --version v1.2-sizing-v2 --side LONG --regime bull
python3 trading/audit_ml_dataset.py --from 2026-07-01 --to 2026-07-31 --min-sample 60
python3 trading/audit_ml_dataset.py --strict
python3 trading/audit_ml_dataset.py --output /tmp/binancebot_ml_dataset_audit
```

Without `--output` or `--manifest`, the command writes nothing. Output uses atomic replacement and includes source hashes, commit, schema, options and fingerprint. Repeating the same inputs/options yields the same fingerprint and logical content except `generated_at`.

`--strict` returns non-zero when baseline readiness criteria fail. Normal mode returns non-zero only for execution errors.

## Generated artifacts

When explicitly requested: `summary.json`, `manifest.jsonl`, `feature_report.json`, `label_report.json`, `leakage_report.json`, `duplicates_report.json`, `temporal_split.json`, `segment_coverage.json`, `excluded_reasons.json` and a generated README.

Generated artifacts are analysis outputs and must not be committed without a separate decision. The general auditor only checks an existing default artifact for corruption/staleness; absence is not an operational warning.

## Next step

The reproducible statistical baseline now consumes only `TRUSTED` closed samples; see `STATISTICAL_BASELINE.md`. It establishes grouped temporal, feature-stability and version-aware evidence before XGBoost.

The CPU-only comparison in `XGBOOST_EXPERIMENT.md` remains offline and cannot change trading or authorize shadow mode.

Future openings may carry optional feature schema v2 context captured before order submission. Historical v1 rows remain valid and are never backfilled. See `FEATURE_REGISTRY.md`.
