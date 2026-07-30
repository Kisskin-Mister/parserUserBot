#!/usr/bin/env bash
set -Eeuo pipefail
cd /mnt/ssd/projects/parserUserBot
set -a
source /mnt/ssd/projects/parserUserBot/.env
set +a
export SESSION_NAME=/mnt/ssd/projects/parserUserBot/session/my_account
export TELEGRAM_PROXY_SCHEME=socks5
export TELEGRAM_PROXY_HOST=127.0.0.1
export TELEGRAM_PROXY_PORT=2080
export OPENAI_BASE_URL=http://127.0.0.1:8642/v1
export DATABASE_URL="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:55432/${POSTGRES_DB}"
exec /mnt/ssd/projects/parserUserBot/.venv/bin/uvicorn api:app --host 127.0.0.1 --port "${APP_PORT:-8000}"
