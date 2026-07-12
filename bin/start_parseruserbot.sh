#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$PROJECT_DIR/.venv/bin/python"
TRIGGER_PLIST_SRC="$PROJECT_DIR/launchd/com.kisskin.parseruserbot.trigger.plist"
DISPATCHER_PLIST_SRC="$PROJECT_DIR/launchd/com.kisskin.parseruserbot.dispatcher.plist"
ENV_FILE="$PROJECT_DIR/.env"
MODE="install"
TRIGGER_MODE=""
DISPATCHER_DRY_RUN=0

usage() {
  cat <<'EOF'
Usage:
  bin/start_parseruserbot.sh [--dry-run-trigger] [--once-trigger] [--dispatcher-dry-run] [--no-launchd]

Options:
  --dry-run-trigger    Run trigger_worker once in dry-run mode before launchd reload
  --once-trigger       Run trigger_worker once in normal mode before launchd reload
  --dispatcher-dry-run Run callback_dispatcher once in dry-run mode before launchd reload
  --no-launchd         Do not install/reload launchd plists
  -h, --help           Show this help
EOF
}

status() { printf '[parserUserBot] %s\n' "$*"; }
fail() { printf '[parserUserBot][ERROR] %s\n' "$*" >&2; exit 1; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "command not found: $1"
}

require_env_value() {
  local name="$1"
  local value="${!name:-}"
  [[ -n "$value" ]] || fail "required env var is empty: $name"
}

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

run_trigger_once() {
  local mode="$1"
  local -a cmd=("$VENV_PY" "$PROJECT_DIR/trigger_worker.py" --once)
  if [[ "$mode" == "dry-run" ]]; then
    cmd+=(--dry-run)
  fi
  status "running trigger_worker (${mode})"
  (
    cd "$PROJECT_DIR"
    load_env
    "${cmd[@]}"
  )
}

run_dispatcher_once() {
  local -a cmd=("$VENV_PY" "$PROJECT_DIR/callback_dispatcher.py" --once)
  if [[ "$DISPATCHER_DRY_RUN" -eq 1 ]]; then
    cmd+=(--dry-run)
  fi
  status "running callback_dispatcher ($( [[ "$DISPATCHER_DRY_RUN" -eq 1 ]] && echo dry-run || echo real ))"
  (
    cd "$PROJECT_DIR"
    load_env
    "${cmd[@]}"
  )
}

install_one_launchd() {
  local plist_src="$1"
  local label="$2"
  local target="$HOME/Library/LaunchAgents/${label}.plist"
  cp "$plist_src" "$target"

  if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
    status "reloading launchd job $label"
    launchctl bootout "gui/$(id -u)" "$target" >/dev/null 2>&1 || true
  else
    status "installing launchd job $label"
  fi

  launchctl bootstrap "gui/$(id -u)" "$target"
  launchctl kickstart -k "gui/$(id -u)/$label"
  status "launchd job active: $label"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run-trigger) TRIGGER_MODE="dry-run" ;;
    --once-trigger) TRIGGER_MODE="once" ;;
    --dispatcher-dry-run) DISPATCHER_DRY_RUN=1 ;;
    --no-launchd) MODE="no-launchd" ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown argument: $1" ;;
  esac
  shift
done

require_cmd launchctl
require_cmd bash
require_cmd openclaw
[[ -f "$ENV_FILE" ]] || fail ".env not found: $ENV_FILE"
[[ -x "$VENV_PY" ]] || fail "venv python not found: $VENV_PY"
[[ -f "$TRIGGER_PLIST_SRC" ]] || fail "trigger plist not found: $TRIGGER_PLIST_SRC"
[[ -f "$DISPATCHER_PLIST_SRC" ]] || fail "dispatcher plist not found: $DISPATCHER_PLIST_SRC"

load_env
require_env_value API_ID
require_env_value API_HASH
require_env_value PHONE_NUMBER
require_env_value LOG_CHAT_ID
require_env_value DATABASE_URL
require_env_value OPENCLAW_CALLBACK_SESSION_KEY

mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_DIR/logs" "$OPENCLAW_CALLBACK_SPOOL_DIR" "$OPENCLAW_CALLBACK_SENT_DIR" "$OPENCLAW_CALLBACK_FAILED_DIR"

status "project dir: $PROJECT_DIR"
status "callback target: $OPENCLAW_CALLBACK_SESSION_KEY"
status "spool dir: $OPENCLAW_CALLBACK_SPOOL_DIR"

if [[ -n "$TRIGGER_MODE" ]]; then
  run_trigger_once "$TRIGGER_MODE"
fi

run_dispatcher_once

if [[ "$MODE" != "no-launchd" ]]; then
  install_one_launchd "$TRIGGER_PLIST_SRC" "com.kisskin.parseruserbot.trigger"
  install_one_launchd "$DISPATCHER_PLIST_SRC" "com.kisskin.parseruserbot.dispatcher"
else
  status "launchd install skipped (--no-launchd)"
fi

status "done"
