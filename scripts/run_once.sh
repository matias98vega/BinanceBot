#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$BOT_DIR"

export PYTHONIOENCODING=utf-8
exec "$BOT_DIR/.venv/bin/python" "$BOT_DIR/trading/bot.py"
