# Architecture index

The detailed runtime architecture remains in [`../ARCHITECTURE.md`](../ARCHITECTURE.md). Functional identity is intentionally layered: runtime behavior uses `bot_version`; rule identity uses `strategy_version`; persistence and feature schemas evolve independently; offline/shadow models use `model_version`; deployed non-behavioral capabilities use the canonical epochs in [`VERSIONING_POLICY.md`](VERSIONING_POLICY.md).

No capability registry import is part of the live execution path. `trading/check_version_consistency.py` is a read-only maintenance tool.

## Pre-entry evidence boundary

`pre_entry_gate_observability.py` observes the pure gate result and writes sanitized append-only evidence through `pre_entry_gate_evidence.py`. Offline policy simulation lives in `pre_entry_tolerance_shadow.py`; the gate does not import it. `CycleRunner` supplies only already-observed position prices and records a same-cycle outcome after LONG/SHORT open attempts. No extra exchange read, decision feedback or payload mutation exists.
