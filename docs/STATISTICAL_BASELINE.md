# Reproducible statistical baseline

Run with `.venv/bin/python trading/run_statistical_baseline.py`. The tool is offline and read-only unless `--output PATH` is supplied.

The sample unit is one closed base `trade_id` classified `TRUSTED` by the ML dataset audit. Open, PARTIAL, EXCLUDED and partial close events are not samples. Bot version belongs to the opening.

Dependency groups use connected position overlap plus a one-hour UTC opening window and remain intact across chronological train/validation/final-test boundaries. The initial target is 60/20/20, adjusted to group boundaries.

Models are majority class, train prior, side, regime, side x regime, regularized logistic regression and a depth-two tree. Preprocessing is learned on train only. Labels, closing facts, identifiers and post-trade analytics are forbidden.

Stability reports PSI with train-derived bins, KS and unseen categories. Evaluation includes classification, calibration and economic retention metrics. All conclusions are exploratory, non-causal and disconnected from live trading.

```bash
.venv/bin/python trading/run_statistical_baseline.py --json
.venv/bin/python trading/run_statistical_baseline.py --walk-forward
.venv/bin/python trading/run_statistical_baseline.py --output /tmp/binancebot_statistical_baseline
```

XGBoost and live scoring remain outside this tool. `XGBOOST_EXPERIMENT.md` reuses this exact split without mutating it.
