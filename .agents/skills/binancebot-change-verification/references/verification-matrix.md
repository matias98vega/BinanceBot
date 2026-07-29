# BinanceBot verification matrix

Use the highest level required by any changed file or changed behavior. Inspect
diff content before choosing a row. `Targeted` means the smallest reliable
offline test selection; use the full suite when no reliable target exists and
the row requires broad coverage.

## Canonical commands

Verify the interpreter first:

```bash
test -x .venv/bin/python
```

Compilation:

```bash
.venv/bin/python -m py_compile trading/*.py
.venv/bin/python -m py_compile trading/orchestration/*.py
.venv/bin/python -m py_compile dashboard/app.py
```

Suite:

```bash
.venv/bin/python -m unittest discover -s trading
```

Consistency and audit:

```bash
.venv/bin/python trading/check_version_consistency.py --strict
.venv/bin/python trading/audit_data_quality.py
.venv/bin/python trading/audit_ml_dataset.py
.venv/bin/python trading/audit_feature_semantics.py
```

Git:

```bash
git diff --check
git status --short
git diff --stat
```

Do not substitute `python` or `python3`. Do not install missing dependencies.

## Selection matrix

| Type | Level | Compile | Tests | Auditor | Consistency | Additional protection | Escalate when |
|---|---|---|---|---|---|---|---|
| Docs-only | LEVEL_1_LIGHT | No | No | No | If version claims change | Validate links, structure, diff | Instructions or behavior claims change |
| Trading Python isolated | LEVEL_2_TARGETED | Trading | Targeted | Relevant only | If behavior/version touched | Integrity before/after | Lifecycle, persistence, risk, or broad imports |
| Analytics/accounting | LEVEL_3_FULL | Trading | Full | Data quality; ML/semantics when relevant | Strict | Stable ledgers and analytics hashes | Formula, schema, ROI, PnL, or source-of-truth changes |
| Reconciliation/recovery | LEVEL_3_FULL | Trading | Full | Data quality | Strict if semantics change | State/history integrity; offline client proof | Any write path or Binance-facing behavior |
| Orchestration | LEVEL_3_FULL | Trading and orchestration | Full | Applicable audits | Strict | Active/stable snapshots | Cross-cycle, persistence, scheduling, or mixed changes |
| Telegram | LEVEL_2_TARGETED | Trading | Targeted | Relevant only | If version output changes | Confirm read-only behavior | Trading source, write callback, or cross-cutting change |
| Dashboard | LEVEL_2_TARGETED | Dashboard | Targeted if available | Relevant only | If version output changes | Confirm read-only behavior | Trading source, writes, auth, or cross-cutting change |
| Config | LEVEL_1_LIGHT | Affected Python if parsed | Targeted if semantics change | No | If capability/version changes | Secrets and defaults review | Risk, network, scheduling, capital, or behavior changes |
| Versioning/capabilities | LEVEL_3_FULL | Trading | Full | Data quality when historical | Strict | No backfill; opening-version semantics | Runtime behavior or compatibility changes |
| Tests-only | LEVEL_2_TARGETED | Affected area | Changed tests plus needed regression | Relevant only | If fixtures include versions | Tempdirs, injected paths, Fake/Replay | Tests can reach runtime paths, network, or production clients |
| CLI/auditor read-only | LEVEL_2_TARGETED | Trading | Targeted | Run changed CLI only if proven offline | If version-aware | Hash protected files | Write flags, repairs, network, or ambiguous side effects |
| Data/historical | BLOCKED | No | No | No | No | Require explicit authorization and safety procedure | Always; resume only with authorized exact scope |
| Binance-facing | LEVEL_3_FULL | Trading | Full offline only | Applicable audits | Strict if behavioral | Prove Fake/Replay isolation | Block if network, POST, credentials, or live client is possible |
| Mixed | LEVEL_3_FULL | All affected areas | Full | All applicable | Strict when relevant | Full integrity comparison | Any unresolved scope or attribution |

Risk/safety/pre-entry and lifecycle changes use `LEVEL_3_FULL` even when isolated
to one file. Runtime or historical files never become safe merely because they
appear beside code changes.
