---
name: binancebot-change-verification
description: >-
  Use when finishing, validating, or preparing to commit implemented changes in
  the BinanceBot repository, including explicit $binancebot-change-verification
  requests and requests to verify, close a repair, leave a change ready for
  commit, or validate before an explicitly requested commit or push workflow.
  Classify the actual diff, run proportional BinanceBot checks, and verify that
  tests did not modify runtime or historical data. Do not use for planning
  without changes, explanations, investigation, code reading, prompt generation,
  monitoring, general questions, manual operations, or work outside BinanceBot.
---

# BinanceBot Change Verification

## Authority and scope

Treat this skill as a verification procedure, never as authorization. Run
before commit-work. Do not stage, commit, or push.

1. Read every applicable `AGENTS.md` before acting.
2. Obey the task scope and repository instructions over this procedure.
3. Do not modify implementation, expectations, production data, or environment.
4. Do not commit, push, deploy, restart, repair, clean, reset, checkout, or stash.
5. Do not install dependencies.
6. Use only local, offline evidence.

Complement general verification skills by adding BinanceBot-specific diff
classification, commands, protected-data checks, and evidence. Return
`VERIFICATION_FAILED` first; use systematic-debugging in the subsequent debugging
phase. Use commit workflows only after this skill reports a commit-ready result.
When skills activate together, assign visual implementation to the
frontend/design skill, visual and web review to web-design-guidelines, and
technical validation and repository protection to binancebot-change-verification.

Version: `1.0.0`.

- Patch: correct commands, paths, wording, or classifications.
- Minor: add compatible categories, checks, or auditors.
- Major: change activation, decisions, scripts, or incompatible protection policy.

## Workflow

Follow the sequence without skipping ahead.

### 1. Load instructions

- Resolve the repository root.
- Read the global and repository `AGENTS.md` chain that applies to each changed file.
- Record restrictions from the current task.
- Stop if the directory is not BinanceBot.

### 2. Inspect repository state

- Inspect `git status --short`, the current branch, and HEAD.
- Inspect staged and unstaged diffs, including untracked files.
- Record pre-existing or unrelated changes.
- Never infer scope solely from the task description.

### 3. Classify the actual diff

Classify every changed file and inspect changed content, not only its path.
Cover trading core Python, `trading/orchestration`, analytics/accounting,
reconciliation/recovery, risk/safety/pre-entry, Telegram, Dashboard, tests,
documentation, configuration, versioning/capabilities, read-only tooling,
runtime/historical data, Binance-facing scripts, and mixed changes.

Read [verification-matrix.md](references/verification-matrix.md) after
classification. Use it to select checks and escalation.

### 4. Detect scope violations

Compare changed content with the task's explicit authorization.

Emit `VERIFICATION_BLOCKED_SCOPE_VIOLATION` if an unauthorized diff changes
scoring, sizing, exposure, leverage, TP, SL, Guardian, circuit breakers,
`BinanceClient`, Binance payloads, orders, transfers, rebalance behavior, timers,
scheduling, historical compatibility, or bot-version semantics.

Block sensitive runtime or historical data changes unless the task explicitly
authorizes them and defines a safety procedure. Passing tests never overrides a
scope violation.

### 5. Select one verification level

Select the highest level required by any changed file or changed behavior.

#### LEVEL_0_INSPECTION

Use as the mandatory baseline for every activated verification; never treat inspection without implemented changes as an independent activation reason.

- Read applicable AGENTS.
- Inspect status and all diff forms.
- Run diff whitespace validation.
- Identify unrelated and protected files.

#### LEVEL_1_LIGHT

Use for documentation, agent configuration, formatting, and similarly passive
changes.

- Perform LEVEL_0.
- Validate structure, paths, formatting, and references.
- Run version consistency only when the content affects versions or capabilities.

#### LEVEL_2_TARGETED

Use for isolated implementation, tests, Telegram, Dashboard, or read-only tools
with bounded impact.

- Perform LEVEL_0.
- Compile the affected area.
- Run targeted tests when a reliable target exists.
- Run the relevant auditor.
- Capture protected-file integrity before and after commands.

#### LEVEL_3_FULL

Use for orchestration, lifecycle, persistence, analytics/accounting,
reconciliation, risk/safety, Binance-facing code, behavioral versioning, and
mixed cross-cutting changes.

- Perform LEVEL_0.
- Run all applicable compilation and suite checks from the matrix.
- Run applicable consistency and audit commands.
- Capture and compare protected-file integrity.
- Block any command whose offline isolation is not demonstrated.

Do not run the full suite automatically for documentation-only changes. Do not
repeat checks already run against the same unchanged diff with fresh evidence.

### 6. Prepare evidence storage

Use `/tmp/binancebot-change-verification/` for manifests and long logs. Keep
repository files untouched.

Before verification commands, record for every existing protected file:

- SHA-256;
- byte size;
- inode;
- mtime;
- line count for JSONL when practical.

Identify each snapshot with repository HEAD, diff fingerprint, and capture time.
Do not store credentials, signatures, authenticated payloads, or raw secrets.

### 7. Protect normally stable files

Treat as normally stable: `data/history/trades.jsonl`,
`data/history/features.jsonl`, `data/history/capital_ledger.jsonl`,
`data/history/rebalance_status.json`, `trading/trade_analytics.jsonl`, and
`data/history/pre_entry_gate_evidence.jsonl` when present.

Any unexplained change during verification is an unexpected write until proven
otherwise.

### 8. Observe active concurrent files

Treat as potentially active: `data/history/timeline.jsonl`,
`data/history/operational_state.jsonl`, `data/history/decisions.jsonl`,
`data/history/snapshots.jsonl`, `trading/decision_snapshots.jsonl`,
`trading/state.json`, and `trading/bot_state.json`.

A hash change alone does not prove normal runtime activity. Inspect inode,
growth, mtime, timestamps, cycle or event identifiers, absence of fixture IDs,
and evidence of truncation before classifying it.

### 9. Execute only pertinent checks

- Verify `test -x .venv/bin/python` before any Python command.
- Use `.venv/bin/python` exclusively for project Python commands.
- Never use `python` or `python3` for project verification.
- Never install or upgrade dependencies.
- Run only commands selected from the matrix and confirmed offline.
- Preserve command, exit code, concise result, and long-log path.
- Avoid commands that can write production state as a side effect.

Review new tests for temporary directories, injected paths, and FakeBinanceClient,
ReplayClient, or equivalent offline isolation.

### 10. Recheck integrity and concurrent changes

Repeat the protected-file snapshot after verification. Classify every difference:

- `UNCHANGED`;
- `EXPECTED_CONCURRENT_RUNTIME_ACTIVITY`;
- `UNEXPECTED_TEST_WRITE`;
- `TRUNCATION_OR_REWRITE_RISK`;
- `INDETERMINATE`.

Require affirmative evidence for expected concurrent activity. Treat shrinking,
inode replacement, malformed append boundaries, fixture IDs, or unexplained
stable-file changes as risk.

Never restore, truncate, clean, or rewrite a changed file automatically.

### 11. Handle unsafe or incomplete verification

#### Real Binance or network risk

- Never execute orders, closes, cancellations, transfers, rebalances, write
  reconciliations, operational scripts, or clients without demonstrated isolation.
- Never run a dry-run that may perform a POST.
- Mark a network-dependent check `SKIPPED_NETWORK_REQUIRED`.
- Block possible real Binance access with
  `VERIFICATION_BLOCKED_REAL_BINANCE_RISK`.

#### Test failure

- Preserve output and exit code.
- Show the first useful cause and log path.
- Retry at most once only when evidence indicates flakiness.
- Do not modify code or expectations.
- Return `VERIFICATION_FAILED`.
- Recommend systematic debugging as a separate task.

#### Missing dependency

- Do not install it.
- Return `VERIFICATION_BLOCKED` with reason
  `VERIFICATION_BLOCKED_DEPENDENCY`.

#### Limited environment

- Run every safe applicable check.
- Return `VERIFICATION_PARTIAL` with reason
  `VERIFICATION_PARTIAL_ENVIRONMENT_LIMITATION`.
- Never claim complete success.

#### Unrelated dirty tree

- Do not reset, checkout, stash, clean, or stage.
- Separate unrelated changes in the report.
- Block when they overlap the verified diff or make attribution indeterminate.

### 12. Decide

Use exactly one primary decision:

- `READY_FOR_COMMIT`;
- `VERIFICATION_PASSED_NOT_COMMIT_READY`;
- `VERIFICATION_PARTIAL`;
- `VERIFICATION_FAILED`;
- `VERIFICATION_BLOCKED`.

Require all of the following for `READY_FOR_COMMIT`:

- every required verification passed;
- no scope violation or unexplained contamination exists;
- no material limitation remains;
- the complete working tree is understood;
- no production or Binance access occurred;
- evidence is recent and matches the current diff.

Use `VERIFICATION_PASSED_NOT_COMMIT_READY` when checks pass but the task did not
request commit preparation, the tree intentionally contains separate work, or
another non-failing prerequisite remains.

## Output format

Return a compact report:

```text
BINANCEBOT CHANGE VERIFICATION

Scope:
- Changed areas:
- Risk level:
- Verification level:

Checks:
- [PASS] command — summary
- [FAIL] command — summary
- [SKIP] check — reason

Data safety:
- Stable protected files:
- Concurrent runtime changes:
- Unexpected writes:
- Production/Binance access:

Limitations:
- ...

Decision:
READY_FOR_COMMIT | VERIFICATION_PASSED_NOT_COMMIT_READY |
VERIFICATION_PARTIAL | VERIFICATION_FAILED | VERIFICATION_BLOCKED

Evidence:
- Tests:
- Auditor:
- Version consistency:
- git diff --check:
- Working tree:
```

Do not print full logs. Report the first actionable cause and the corresponding
path under `/tmp/binancebot-change-verification/`.
