#!/usr/bin/env bash
set -euo pipefail

BOT_DIR="${BOT_DIR:-/opt/BinanceBot}"
cd "$BOT_DIR"

exec "$BOT_DIR/.venv/bin/python" "$BOT_DIR/trading/preflight_check.py"
