#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

sudo apt update
sudo apt install -y python3 python3-venv python3-pip git ca-certificates

cd "$BOT_DIR"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created $BOT_DIR/.env. Edit it before running the bot."
fi

echo "Install complete."
echo "Next:"
echo "  nano $BOT_DIR/.env"
echo "  $BOT_DIR/.venv/bin/python $BOT_DIR/trading/setup_check.py"
