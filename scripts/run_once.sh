#!/usr/bin/env bash
set -euo pipefail

BOT_DIR="${BOT_DIR:-/opt/BinanceBot}"
cd "$BOT_DIR"

export PYTHONIOENCODING=utf-8
exec "$BOT_DIR/.venv/bin/python" "$BOT_DIR/trading/bot.py"
