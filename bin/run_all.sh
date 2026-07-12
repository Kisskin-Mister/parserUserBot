#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$PROJECT_DIR/.venv/bin/python"

cd "$PROJECT_DIR"

bin/start_parseruserbot.sh "$@"
exec "$VENV_PY" main.py
