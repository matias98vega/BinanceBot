# XGBoost experiment — OFFLINE ONLY

This CPU-only experiment is not connected to scoring, sizing, filters, orders
or shadow mode. It uses `tree_method="hist"`, `device="cpu"` and `n_jobs=1`.

The dataset is exactly the closed `TRUSTED` manifest used by the statistical
baseline. Dependency groups and chronological boundaries are reused;
validation alone ranks nine predefined candidates and test is opened only for
the selected model.

Feature sets are conservative stable categoricals, stable-plus-numeric and an
explicit exploratory ATR/RSI/score set. A 0.02 log-loss noise guard prevents a
small shifted-feature improvement from winning automatically.

Early stopping uses validation, never test. Evaluation includes grouped
walk-forward, classification/economic metrics, calibration, importances and a
1,000-iteration dependency-group bootstrap against the baseline.

```bash
.venv/bin/python trading/run_xgboost_experiment.py --explain
.venv/bin/python trading/run_xgboost_experiment.py --walk-forward
.venv/bin/python trading/run_xgboost_experiment.py --output /tmp/binancebot_xgboost_experiment
```

No binary model is persisted. Artifacts are labelled `OFFLINE_ONLY`,
`NOT_FOR_TRADING` and `NOT_SHADOW_APPROVED`. A negative result keeps shadow
mode blocked and is retained as evidence.
