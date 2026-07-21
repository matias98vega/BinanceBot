# Architecture index

The detailed runtime architecture remains in [`../ARCHITECTURE.md`](../ARCHITECTURE.md). Functional identity is intentionally layered: runtime behavior uses `bot_version`; rule identity uses `strategy_version`; persistence and feature schemas evolve independently; offline/shadow models use `model_version`; deployed non-behavioral capabilities use the canonical epochs in [`VERSIONING_POLICY.md`](VERSIONING_POLICY.md).

No capability registry import is part of the live execution path. `trading/check_version_consistency.py` is a read-only maintenance tool.
