#!/usr/bin/env bash
# cowork-dash launcher. Sets up a venv on first run, then starts the daemon.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV="$SCRIPT_DIR/.venv"
PYTHON="${PYTHON:-python3}"

if [ ! -d "$VENV" ]; then
  echo "[cowork-dash] creating venv at $VENV"
  "$PYTHON" -m venv "$VENV"
  "$VENV/bin/pip" install --upgrade pip wheel >/dev/null
  "$VENV/bin/pip" install -r requirements.txt
fi

# Ensure config exists
CONFIG_DIR="$HOME/.cowork-dash"
CONFIG_FILE="$CONFIG_DIR/config.toml"
if [ ! -f "$CONFIG_FILE" ]; then
  mkdir -p "$CONFIG_DIR"
  cp config.example.toml "$CONFIG_FILE"
  echo "[cowork-dash] wrote starter config to $CONFIG_FILE"
  echo "[cowork-dash] edit it (add your GitHub token + username), then re-run."
  exit 1
fi

exec "$VENV/bin/python" daemon.py
