# Sanitized replay incident fixtures

This is a permanent offline regression library built from incidents and incident-driven fixes. It contains no credentials, raw account payloads, or production-ready market data. A fixture is evidence about control flow, not proof that Binance would reproduce the same response today.

Every fixture carries `replay_schema_version` and `fixture_schema_version`, and separates observed provenance, sanitization, assumptions, missing fields, inferred fields, and synthetic fields. Validation requires:

```text
NOT_PRODUCTION_DATA
SANITIZED
OFFLINE_ONLY
NO_NETWORK
NOT_FOR_PNL_VALIDATION
```

## Fidelity

- `FULL_FIDELITY`: complete original inputs; cannot declare missing fields.
- `CONTROL_FLOW_FIDELITY`: synthetic values preserve the safety branch under test.
- `PARTIAL_OBSERVATION`: replays only recorded observations that still exist.
- `ANALYTICS_ONLY`: supports event analytics, not an exchange cycle.

Confidence (`HIGH`, `MEDIUM`, `LOW`) describes confidence in the incident classification, not accuracy of synthetic prices or quantities. No current incident fixture is `FULL_FIDELITY`.

## Permanent incident matrix

| Scenario | Fidelity | Confidence | Preserved evidence | Explicit limitation |
|---|---|---:|---|---|
| `incident-ada-stale-spot` | CONTROL_FLOW_FIDELITY | HIGH | reconciliation class, reason, observed timestamp | raw account, trades, orders unavailable |
| `incident-sol-orphan-futures` | CONTROL_FLOW_FIDELITY | MEDIUM | recovery class and regression history | exact position/open-order snapshot unavailable |
| `incident-ondo-xrp-partial-evidence` | ANALYTICS_ONLY | HIGH | trade boundary timestamps and gap class | no contemporaneous runtime evidence |
| `incident-oco-failure-after-fill` | CONTROL_FLOW_FIDELITY | MEDIUM | incident-driven failure branch | exact order/fill/OCO responses unavailable |
| `incident-external-spot-close-with-dust` | CONTROL_FLOW_FIDELITY | MEDIUM | reconciliation class and tested behavior | symbol and exchange transcript unavailable |
| `incident-order-timeout-unknown-result` | CONTROL_FLOW_FIDELITY | LOW | result remains unknown | request ID, query and final result unavailable |

JSON fixtures live in `trading/testing/fixtures/incidents/`. Loaded fixtures are immutable and have deterministic fixture and ReplayTape fingerprints.

## Commands

```bash
.venv/bin/python trading/testing/build_replay_fixture.py --validate-all --json
.venv/bin/python trading/testing/build_replay_fixture.py --list
.venv/bin/python trading/testing/run_replay_scenario.py --incident incident-ada-stale-spot --json
```

`--strict` intentionally rejects every current incident because each declares missing evidence. Export occurs only with an explicit `--output` directory. The builder reads committed fixtures; it does not scrape or rewrite production history.

## Adding an incident

1. Preserve only the minimum observed provenance needed to identify the failure class.
2. Sanitize identifiers and monetary values; document every transformation.
3. Declare all missing, inferred, and synthetic fields. Never upgrade fidelity to hide missing inputs.
4. State expected and forbidden behavior, including writes and PnL restrictions.
5. Add a deterministic offline regression and validate the entire library.

Fixtures must never be used for PnL validation, strategy optimization, live order construction, or as a fallback source for production state.
