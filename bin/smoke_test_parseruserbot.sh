#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$PROJECT_DIR/.venv/bin/python"
ENV_FILE="$PROJECT_DIR/.env"
SPOOL_DIR="$PROJECT_DIR/runtime/callback_spool"
SENT_DIR="$PROJECT_DIR/runtime/callback_sent"
FAILED_DIR="$PROJECT_DIR/runtime/callback_failed"
RUN_REAL_TRIGGER=0

usage() {
  cat <<'EOF'
Usage:
  bin/smoke_test_parseruserbot.sh [--real-trigger]

Options:
  --real-trigger  Run trigger_worker --once without --dry-run
  -h, --help      Show this help
EOF
}

status() { printf '[parserUserBot smoke] %s\n' "$*"; }
fail() { printf '[parserUserBot smoke][ERROR] %s\n' "$*" >&2; exit 1; }

load_env() {
  set -a
  source "$ENV_FILE"
  : "${OPENCLAW_CALLBACK_SESSION_KEY:=agent:main:main}"
  : "${OPENCLAW_CALLBACK_SPOOL_DIR:=$PROJECT_DIR/runtime/callback_spool}"
  : "${OPENCLAW_CALLBACK_SENT_DIR:=$PROJECT_DIR/runtime/callback_sent}"
  : "${OPENCLAW_CALLBACK_FAILED_DIR:=$PROJECT_DIR/runtime/callback_failed}"
  export OPENCLAW_CALLBACK_SESSION_KEY OPENCLAW_CALLBACK_SPOOL_DIR OPENCLAW_CALLBACK_SENT_DIR OPENCLAW_CALLBACK_FAILED_DIR
  set +a
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --real-trigger) RUN_REAL_TRIGGER=1 ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown argument: $1" ;;
  esac
  shift
done

[[ -f "$ENV_FILE" ]] || fail ".env not found: $ENV_FILE"
[[ -x "$VENV_PY" ]] || fail "venv python not found: $VENV_PY"
mkdir -p "$SPOOL_DIR" "$SENT_DIR" "$FAILED_DIR"
load_env

status "python version"
"$VENV_PY" --version

status "import/syntax check"
(
  cd "$PROJECT_DIR"
  "$VENV_PY" -m py_compile main.py database.py dashboard.py mcp_server.py classifier.py message_policy.py trigger_worker.py callback_dispatcher.py
)

status "database connection check"
(
  cd "$PROJECT_DIR"
  load_env
  "$VENV_PY" - <<'PY'
import asyncio
from database import Database

async def main():
    db = Database()
    await db.init()
    async with db.pool.acquire() as conn:
        await conn.fetchval('SELECT 1')
    print('DB_OK')

asyncio.run(main())
PY
)

status "trigger_worker dry-run"
(
  cd "$PROJECT_DIR"
  load_env
  "$VENV_PY" trigger_worker.py --once --dry-run
)

if [[ "$RUN_REAL_TRIGGER" -eq 1 ]]; then
  status "trigger_worker real once"
  (
    cd "$PROJECT_DIR"
    load_env
    "$VENV_PY" trigger_worker.py --once
  )
fi

status "sessions_send-compatible envelope/spool format check"
(
  cd "$PROJECT_DIR"
  load_env
  "$VENV_PY" - <<'PY'
import json
from pathlib import Path
from trigger_worker import CallbackSpool

spool = CallbackSpool()
payload = {"source": "smoke", "entity_type": "vacancies", "count": 1, "fingerprint": "smoke-fp", "items": [{"id": 1}]}
target = spool.write(payload, idempotency_key='smoke:test:callback', dry_run=False)
data = json.loads(Path(target).read_text(encoding='utf-8'))
assert data['sessionKey'] == 'agent:main:main', data
assert data['message'].startswith('[SCRIPT_CALLBACK] '), data
assert data['status'] == 'pending', data
print('CALLBACK_FORMAT_OK')
PY
)

status "dispatcher dry-run"
(
  cd "$PROJECT_DIR"
  load_env
  "$VENV_PY" callback_dispatcher.py --once --dry-run
)

status "dispatcher real send path check"
(
  cd "$PROJECT_DIR"
  load_env
  "$VENV_PY" - <<'PY'
import json
from pathlib import Path
from unittest.mock import patch
import callback_dispatcher

spool_dir = Path('runtime/callback_spool')
sent_dir = Path('runtime/callback_sent')
failed_dir = Path('runtime/callback_failed')
spool_dir.mkdir(parents=True, exist_ok=True)
sent_dir.mkdir(parents=True, exist_ok=True)
failed_dir.mkdir(parents=True, exist_ok=True)

sample = spool_dir / 'smoke_dispatch.json'
sample.write_text(json.dumps({
    'sessionKey': 'agent:main:main',
    'message': '[SCRIPT_CALLBACK] smoke dispatch',
    'idempotencyKey': 'smoke:dispatch:test'
}, ensure_ascii=False), encoding='utf-8')

dispatcher = callback_dispatcher.SpoolDispatcher()
with patch('subprocess.run') as run_mock:
    run_mock.return_value.returncode = 0
    ok = dispatcher.dispatch_file(sample, dry_run=False)
assert ok is True
assert (sent_dir / 'smoke_dispatch.json').exists()
print('DISPATCH_OK')
(sent_dir / 'smoke_dispatch.json').unlink(missing_ok=True)
PY
)

status "done"
