# Code Review: parserUserBot

**Reviewer:** Hermes Agent (automated)
**Date:** 2026-07-12
**Target Platform:** Raspberry Pi (ARM64 Linux) + Hermes HR Agent integration

---

## 1. Architecture Overview

The project is a **Telethon/Pyrogram-based Telegram user-bot** that monitors channels for job vacancies (Java/Go developer roles), classifies them, and dispatches application callbacks through an agent chain.

### Components

| File | Role | Runtime |
|---|---|---|
| `main.py` | Pyrogram client — monitors Telegram, classifies messages, stores in DB, drives live dashboard | Long-running daemon |
| `classifier.py` | Keyword-based vacancy/news classifier using regex | Called by main.py |
| `message_policy.py` | Determines if a message is from a recruiter DM vs group | Called by main.py |
| `database.py` | asyncpg PostgreSQL ORM with full schema auto-migration | Library |
| `mcp_server.py` | CLI tool for agent integration — list/apply/ignore/delete/repost | CLI (one-shot) |
| `trigger_worker.py` | Polls DB for unnotified vacancies, creates callback outbox entries | CLI / cron (launchd) |
| `callback_dispatcher.py` | Sends callback outbox entries through OpenClaw gateway CLI | CLI / cron (launchd) |
| `dashboard.py` | Rich-based TUI dashboard with live stats | Library |

### Data Flow

```
Telegram Channels
       │
       ▼
  main.py (Pyrogram)  ──► classifier.py  ──► database.py (PostgreSQL)
       │                                          │
       │                                    trigger_worker.py (cron)
       │                                          │
       │                                    callback_dispatcher.py (cron)
       │                                          │
       ▼                                          ▼
  Dashboard (TUI)                          OpenClaw Gateway → Agent Chain
                                                  │
                                           mcp_server.py (CLI tool)
```

### Agent Chain

1. **Sentinel** (Qwen sub-agent) — runs `mcp_server.py list` to detect new vacancies
2. **Main Agent** (Codex) — reads `JOB_HUNTER_SKILL.md`, applies to vacancies, curates news
3. **User** (Nazar) — gets notified of recruiter DMs, reviews channel posts

**Assessment:** The architecture is well-layered with clear separation between the Telegram client, classification, persistence, and agent integration layers. The CLI-based MCP server is a pragmatic approach for agent integration.

---

## 2. Code Quality Issues

### 2.1 Critical: `main.py` uses `asyncpg` but imports are inconsistent

`main.py` line 32 calls `db.pool.acquire()` with async context manager and `await conn.fetchval(...)`. This is **asyncpg** syntax (correct). However, `main.py` was originally written for **Telethon** (per README context) but now imports **Pyrogram**. The library migration appears complete but should be verified end-to-end.

### 2.2 `database.py` — `init()` runs DDL on every startup

Lines 20-135: Every `init()` call runs `CREATE TABLE IF NOT EXISTS` and `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for the entire schema. This is:
- **Slow** on cold start (unnecessary round-trips)
- **Acceptable** for a small project but should be replaced with a proper migration tool (e.g., `aerich`, `alembic` with asyncpg) for production

### 2.3 `database.py` — `mark_entities_as_notified` uses f-string table name

```python
table = 'vacancies' if entity_type == 'vacancies' else 'news'
await conn.execute(
    f"UPDATE {table} SET callback_notified_at = CURRENT_TIMESTAMP WHERE id = ANY($1)",
    entity_ids,
)
```

The table name is hardcoded via if/else (not user input), so this is **safe from SQL injection**, but the pattern is fragile. If a new entity type is added, it silently falls through to `'news'`.

### 2.4 `main.py` — `get_job_status()` calls `launchctl` (macOS-only)

Lines 52-57: `get_job_status()` runs `launchctl print`, which is macOS-specific. On Linux/Pi this will always return `"not loaded"`. Similarly, `get_gateway_status()` calls `openclaw gateway status` which may not exist on the Pi.

**Fix:** Replace with `systemctl is-active` on Linux, or detect the platform.

### 2.5 `callback_dispatcher.py` — hardcoded macOS paths

Lines 48-54: `_resolve_openclaw_bin()` searches for `openclaw` at:
- `/path/to/node/bin/openclaw`
- `/opt/homebrew/bin/openclaw`
- `/usr/local/bin/openclaw`

These are all macOS paths. On Raspberry Pi, the binary location will be different. The fallback `shutil.which("openclaw")` should work if it's on PATH, but the explicit paths are dead code on Linux.

### 2.6 `main.py` — `import json` inside loop

Lines 178-179, 197-198: `import json` is done inside the `command_worker` loop body. While Python caches imports, this is a code smell — move to the top of the file.

### 2.7 `mcp_server.py` — `db.init()` called in every function

Every function in `mcp_server.py` calls `await db.init()` before doing work. The `init()` method is idempotent (checks `if not self.pool`), so this is safe but redundant. Better to init once at module level or in a context manager.

### 2.8 `mcp_server.py` — test expects `dedupe_key` parameter but code doesn't pass it

`test_mcp_server.py` line 52 asserts:
```python
self.mock_db.add_command.assert_any_await("send_message", "@recruiter", "Hi there", dedupe_key="vacancy:7:send_message")
```

But `mcp_server.py` line 46-49 calls:
```python
await db.add_command("send_message", recruiter, message_text)
```

No `dedupe_key` parameter is passed. This test **will fail** as-is. The `add_command` method in `database.py` also doesn't accept a `dedupe_key` keyword argument (it only takes `cmd_type, target, data`).

**Status:** Either the test is outdated, or the `add_command` function needs to be updated to accept and use `dedupe_key`.

### 2.9 `classifier.py` — `async` function that does no async work

`classify_post()` is declared `async` but performs only synchronous regex operations. This adds unnecessary coroutine overhead. Either make it synchronous or justify the async for future extensibility.

### 2.10 `main.py` — `catch_up_unread` and `sync_chats_task` are nearly identical

Lines 127-140 and 222-239: Both iterate unread dialogs and process messages. `sync_chats_task` is a superset that also marks chats as read. This duplication should be consolidated.

---

## 3. Security Concerns

### 3.1 🔴 CRITICAL: `.env` file contains real credentials

`.env.example` contains **actual real values**:
- `API_ID=23383794` (real Telegram API ID)
- `API_HASH=<redacted-telegram-api-hash>` (real API hash — this is a secret)
- `PHONE_NUMBER=<redacted-phone-number>` (real value removed for public release)
- `LOG_CHAT_ID=-1002589610632` (real chat ID)

**The `.env.example` file should contain only placeholder values**, not real credentials. The actual `.env` is correctly gitignored, but `.env.example` is committed to the repo.

**Action required:** Replace `.env.example` with placeholder values:
```
API_ID=your_api_id_here
API_HASH=your_api_hash_here
PHONE_NUMBER=your_phone_number_here
```

### 3.2 🟡 `.session` files are gitignored (good)

The `.gitignore` correctly excludes `*.session` and `*.session-journal`. However, the Pyrogram session file (`my_account.session`) will be created in the working directory. On a shared Pi, ensure:
- File permissions are `600` (owner-only read/write)
- The session file is not in a world-readable location

### 3.3 🟡 `mcp_server.py` has no authentication

Anyone with access to the filesystem can run `mcp_server.py apply <id> "text"` and send messages to recruiters on behalf of the user. On a multi-user Raspberry Pi, this is a risk.

**Mitigation:** Add a simple auth check (e.g., verify caller UID or require a token).

### 3.4 🟡 `main.py` — `OWNER_USERNAME` defaults to `"zogpower"`

Line 20: If `OWNER_USERNAME` is not set, recruiter DMs are forwarded to `@zogpower`. This is a reasonable default but should be explicitly documented as intentional.

### 3.5 🟡 Resume PDFs with PII committed to git

`resume.pdf`, `resume.html`, `resumes/resume_main.pdf`, `resumes/resume_nazr.pdf`, `resumes/resume_v2.pdf`, and `resume_java_go_hybrid_2026.md` are all tracked in git. These contain **full personal information** (phone, email, address, work history). If this repo is ever made public or pushed to a shared remote, PII will be exposed.

**Recommendation:** Add `*.pdf`, `*.html`, and resume files to `.gitignore`, and use `git filter-branch` or BFG Repo Cleaner to remove them from history if the repo has been pushed.

### 3.6 🟡 Database connection string in `.env`

The `.env.example` shows the format `postgresql://postgres@host:***@localhost:5432/tg_hr`. On a Pi, ensure PostgreSQL is configured with password authentication and not running on a public interface.

---

## 4. Missing Functionality for HR Agent Use Case

### 4.1 No systemd service files

The project only has macOS `launchd` `.plist` files. For Raspberry Pi deployment, **systemd `.service` files are needed** for:
- `main.py` (the Telegram client daemon)
- `trigger_worker.py` (periodic callback producer)
- `callback_dispatcher.py` (periodic callback sender)

### 4.2 No health check endpoint

`main.py` runs a Rich TUI dashboard (line 254-257), which is useful for local monitoring but:
- Cannot be checked by systemd watchdog
- Cannot be monitored by external tools
- Will not render properly in a headless Pi environment (no TTY)

**Recommendation:** Add a simple HTTP health endpoint or write a PID file with a heartbeat timestamp.

### 4.3 No graceful shutdown

`main.py` lines 260-264: `KeyboardInterrupt` is caught but no cleanup occurs. The Pyrogram client is not explicitly stopped, database pool is not closed, and background tasks are not cancelled.

### 4.4 No rate limiting on recruiter outreach

The `apply_to_vacancy()` function in `mcp_server.py` sends a message + resume immediately. There's no:
- Cooldown between applications to the same recruiter
- Daily application limit
- Delay between message and document send (could trigger Telegram flood wait)

### 4.5 No notification channel for the owner on Pi

The dashboard is TUI-based. On a headless Pi, the owner needs alternative notification methods:
- Telegram notification when a vacancy is found (currently only for recruiter DMs)
- Webhook to Hermes agent

### 4.6 Missing `conftest.py` or `pytest.ini`

Tests exist in `tests/` but there's no `conftest.py` or `pytest.ini`/`pyproject.toml` to configure pytest. The tests also don't have an `__init__.py` file.

### 4.7 No structured logging

All logging uses basic `logging.basicConfig()` with string formatting. For production on a Pi, structured logging (JSON) would be easier to parse and monitor.

---

## 5. Deployment Readiness for Raspberry Pi (ARM64 Linux)

### 5.1 Dependencies — `requirements.txt`

```
pyrogram
tgcrypto
python-dotenv
aio-pydantic
asyncpg
```

**Issues:**
- `tgcrypto` requires C compilation. On ARM64 Linux (Pi), this needs `build-essential` and Python dev headers installed: `apt install build-essential python3-dev`
- `aio-pydantic` is listed but **never imported** anywhere in the code. It's a dead dependency.
- `rich` is imported in `main.py` (line 8) and `dashboard.py` but **not listed in requirements.txt**
- `asyncpg` requires PostgreSQL client libraries: `apt install libpq-dev`
- No version pinning — all packages are unpinned, which risks breaking changes

**Recommended `requirements.txt`:**
```
pyrogram==2.0.106
tgcrypto==1.2.5
python-dotenv==1.0.1
asyncpg==0.30.0
rich==13.9.0
```

### 5.2 PostgreSQL on Pi

The project uses **asyncpg** (PostgreSQL). On a Raspberry Pi:
- Install PostgreSQL: `apt install postgresql`
- Create database: `createdb tg_hr`
- Configure `.env` with `DATABASE_URL=postgresql://user:pass@localhost:5432/tg_hr`
- Consider SQLite as a lighter alternative for a single-user Pi setup (would require rewriting database.py)

### 5.3 Python version

The code uses `dict | None` syntax (line 40 in `main.py`), which requires **Python 3.10+**. Ensure the Pi runs Python 3.10 or newer.

### 5.4 Memory considerations

- Pyrogram client + asyncpg pool + Rich dashboard = moderate memory usage
- On a Pi with 1GB RAM, this should be fine
- The Rich TUI dashboard is wasteful on a headless system — should be disabled or replaced

### 5.5 `openclaw` binary dependency

`callback_dispatcher.py` shells out to `openclaw` CLI. This binary must be installed on the Pi and accessible on PATH. If OpenClaw is not available on ARM64 Linux, the entire callback dispatch chain breaks.

**Mitigation:** Implement a pure-Python HTTP fallback for the gateway call instead of shelling out.

---

## 6. Suggested Improvements

### 6.1 Priority: Create systemd service files

```ini
# /etc/systemd/system/parseruserbot.service
[Unit]
Description=parserUserBot Telegram Client
After=network.target postgresql.service

[Service]
Type=simple
User=parseruserbot
WorkingDirectory=/mnt/ssd/projects/parserUserBot
ExecStart=/mnt/ssd/projects/parserUserBot/.venv/bin/python main.py
Restart=always
RestartSec=10
EnvironmentFile=/mnt/ssd/projects/parserUserBot/.env

[Install]
WantedBy=multi-user.target
```

### 6.2 Priority: Fix `.env.example` — remove real credentials

Replace all values with safe placeholders. The current file leaks real API credentials.

### 6.3 Priority: Add version pinning to requirements.txt

And remove `aio-pydantic` (unused), add `rich` (used but missing).

### 6.4 Add a pure-Python callback dispatch path

Instead of shelling out to `openclaw`, use `aiohttp` or `httpx` to call the gateway HTTP endpoint directly. This removes the external binary dependency.

### 6.5 Replace Rich TUI with headless mode

Add a `--headless` flag to `main.py` that skips the Rich dashboard and instead:
- Writes status to a JSON file periodically
- Logs to stdout/stderr for journald capture

### 6.6 Add `__init__.py` to tests/

And add a `conftest.py` or `pyproject.toml` with pytest configuration.

### 6.7 Consolidate duplicate code

- Merge `catch_up_unread()` and `sync_chats_task()` in `main.py`
- Move `db.init()` to a single initialization point in `mcp_server.py`

### 6.8 Add structured error handling for Telegram API

Pyrogram can raise `FloodWait`, `UserNotParticipant`, etc. The current `except Exception` in `process_message` swallows all errors silently (only updates dashboard). Add specific exception handling for common Telegram API errors.

### 6.9 Add type hints consistently

`database.py` has no return type hints. `mcp_server.py` functions return `dict` but are not typed. Adding type hints would improve maintainability and enable mypy checking.

### 6.10 Consider SQLite for Pi deployment

For a single-user bot on a Pi, PostgreSQL is heavyweight. SQLite with `aiosqlite` would:
- Eliminate the PostgreSQL dependency
- Simplify deployment
- Reduce memory usage
- Require rewriting `database.py` (but the schema is simple enough)

---

## 7. Test Coverage Assessment

| Component | Tests | Coverage |
|---|---|---|
| `classifier.py` | ✅ 3 tests in `test_main_helpers.py` | Good — covers go/java/news classification, negative cases |
| `message_policy.py` | ✅ 3 tests in `test_main_helpers.py` | Good — covers private/group/self messages |
| `mcp_server.py` | ⚠️ 6 tests in `test_mcp_server.py` | Partial — **one test likely broken** (dedupe_key assertion) |
| `trigger_worker.py` | ✅ 2 tests in `test_trigger_worker.py` | Basic — covers payload truncation and outbox enqueue |
| `callback_dispatcher.py` | ✅ 3 tests in `test_callback_dispatcher.py` | Good — covers build_command, success/failure paths |
| `main.py` | ✅ 2 tests in `test_recruiter_*.py` | Covers recruiter sync skip and dedupe |
| `database.py` | ❌ No tests | **Missing** — no DB layer tests at all |
| `dashboard.py` | ❌ No tests | Missing — low priority |
| `main.py` command_worker | ❌ No tests | Missing — the command execution loop is untested |

### Test Issues
1. `test_mcp_server.py` line 52: `add_command` call with `dedupe_key` kwarg doesn't match actual code
2. No `conftest.py` for shared fixtures
3. No CI/CD configuration (no GitHub Actions, no Makefile for `make test`)

---

## 8. Summary of Findings

### 🔴 Must Fix Before Deployment
1. **`.env.example` contains real API credentials** — replace with placeholders immediately
2. **Missing `rich` in requirements.txt** — `main.py` will crash on import
3. **macOS-only `launchctl` calls in `main.py`** — dashboard will show errors on Linux
4. **No systemd service files** — the bot cannot auto-start on Pi
5. **`tgcrypto` needs build tools on ARM64** — document or pre-build wheel

### 🟡 Should Fix
6. `aio-pydantic` is unused — remove from requirements
7. No version pinning in requirements.txt
8. `callback_dispatcher.py` macOS path hardcoding
9. `mcp_server.py` test mismatch (dedupe_key)
10. No graceful shutdown in `main.py`

### 🟢 Nice to Have
11. Replace Rich TUI with headless mode for Pi
12. Add SQLite option as lightweight alternative to PostgreSQL
13. Pure-Python HTTP fallback for OpenClaw gateway calls
14. Structured logging
15. Type hints throughout
16. CI/CD pipeline

---

## 9. Files Created/Modified

- **Created:** `/mnt/ssd/projects/parserUserBot/CODE_REVIEW.md` (this file)

---

*Review completed by Hermes Agent — 2026-07-12*
