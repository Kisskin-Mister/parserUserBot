<div align="center">

# parserUserBot

Telegram user-account parser for Java/Go vacancies and IT news, with a PostgreSQL outbox and CLI tools for agent-driven processing.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![CI](https://github.com/Kisskin-Mister/parserUserBot/actions/workflows/ci.yml/badge.svg)](https://github.com/Kisskin-Mister/parserUserBot/actions/workflows/ci.yml)

</div>

## What this project is

parserUserBot is a Pyrogram-based Telegram user-bot. It logs in as a Telegram user account, reads unread/channel messages available to that account, classifies messages with local keyword rules, stores matches in PostgreSQL, and exposes a small CLI (`mcp_server.py`) that an external agent or operator can use to list, apply, ignore, delete, repost, or report items.

Current real capabilities in code:

- detects Java/Go vacancy posts when a job-context keyword and a tech keyword are both present
- detects IT/news posts using `keywords.txt`
- excludes spam/irrelevant posts using `negative_keywords.txt`
- stores vacancies, news, recruiter interactions, commands, callback batches, and callback outbox rows in PostgreSQL
- forwards incoming private recruiter DMs to `OWNER_USERNAME` once per Telegram `(chat_id, message_id)`
- renders a Rich terminal dashboard while `main.py` is running
- queues application commands and optional resume sending through the DB-backed `commands` table
- queues callback payloads from `trigger_worker.py` and sends them through `callback_dispatcher.py` via `openclaw gateway call chat.send`
- includes Linux systemd and macOS launchd templates

Important limitations:

- this is not a Telegram Bot API bot; it uses a user account through Pyrogram
- `mcp_server.py` is a CLI/tool shim, not a JSON-RPC MCP stdio server
- the database backend in `database.py` is PostgreSQL via `asyncpg`, not SQLite
- there is no web dashboard; the implemented dashboard is a terminal Rich TUI
- classification is deterministic keyword/regex matching, not an ML classifier

## Architecture at a glance

```
Telegram account/channels
        |
        v
main.py (Pyrogram handlers, unread sync, command worker, Rich TUI)
        |
        +--> classifier.py / message_policy.py
        |
        v
PostgreSQL tables from database.py
        |
        +--> trigger_worker.py -> callback_batches/callback_outbox
        |                         |
        |                         v
        |                 callback_dispatcher.py -> openclaw gateway call chat.send
        |
        +--> mcp_server.py CLI -> commands / vacancy_outbox / news updates
```

See `ARCHITECTURE.md` for the exact data flow, schema, classifier logic, and CLI tools mapped to `mcp_server.py`.

## Requirements

- Python 3.10+
- PostgreSQL 14+ (or any PostgreSQL version supported by `asyncpg`)
- Telegram API credentials from https://my.telegram.org/apps
- A Telegram account that is allowed to read the target chats/channels
- Optional: `openclaw` CLI when using callback dispatch integration

## Quick start

```bash
git clone https://github.com/Kisskin-Mister/parserUserBot.git
cd parserUserBot
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
cp .env.example .env
```

Create a PostgreSQL database/user and set `DATABASE_URL` in `.env`:

```bash
createdb parseruserbot
# example .env value:
# DATABASE_URL=postgresql://parseruserbot:***@localhost:5432/parseruserbot
```

Run tests before using your real account:

```bash
pip install -e '.[dev]'
python -m pytest -q
python -m compileall -q .
```

Run the bot once to create the Pyrogram session and authenticate Telegram:

```bash
python main.py
```

On first login Pyrogram asks for the code sent by Telegram and, if enabled, your 2FA password. The generated `*.session` files are ignored by git and must stay private.

## Telegram API credentials

1. Open https://my.telegram.org/apps
2. Log in with the phone number used for the Telegram user account
3. Create an application
4. Copy the numeric `api_id` into `API_ID`
5. Copy `api_hash` into `API_HASH`
6. Put the same phone number into `PHONE_NUMBER`

Do not commit real API credentials, `.env`, or Pyrogram session files. If a credential was committed or shared, rotate it at Telegram immediately.

## Configuration reference

All runtime configuration is loaded from `.env` with `python-dotenv`.

| Variable | Required | Used by | Meaning |
|---|---:|---|---|
| `API_ID` | yes | `main.py` | Telegram API ID from my.telegram.org |
| `API_HASH` | yes | `main.py` | Telegram API hash from my.telegram.org |
| `PHONE_NUMBER` | yes | `main.py` | Telegram account phone number |
| `LOG_CHAT_ID` | usually | `main.py`, `mcp_server.py` | Messages from this chat are ignored by parser; `post` queues messages to it |
| `OWNER_USERNAME` | no | `main.py`, `mcp_server.py` | Recipient for recruiter DM forwards and `report`; default in code is `zogpower` |
| `NEWS_CHAT_ID` | only for news reposts | `mcp_server.py` | Target chat for `repost_news` |
| `DATABASE_URL` | yes | `database.py` | PostgreSQL connection URL passed to `asyncpg.create_pool` |
| `RESUME_PATH` | no | `mcp_server.py` | If set and file exists, `apply` also queues `send_document` |
| `TRIGGER_ENTITY_TYPE` | no | `trigger_worker.py` | Default CLI entity stream: `vacancies` or `news` |
| `TRIGGER_BATCH_LIMIT` | no | `trigger_worker.py` | Max rows per callback batch; default `50` |
| `TRIGGER_POLL_INTERVAL_SEC` | no | `trigger_worker.py` | Sleep interval in non-`--once` mode; default `30` |
| `TRIGGER_LOG_LEVEL` | no | `trigger_worker.py` | Python logging level; default `INFO` |
| `OPENCLAW_CALLBACK_SESSION_KEY` | no | `trigger_worker.py` | Session key embedded in callback message; default `agent:main:main` |
| `OPENCLAW_GATEWAY_URL` | no | `callback_dispatcher.py` | Optional `openclaw gateway call --url` value |
| `OPENCLAW_GATEWAY_TOKEN` | no | `callback_dispatcher.py` | Optional `openclaw gateway call --token` value |
| `OPENCLAW_CALLBACK_TIMEOUT_MS` | no | `callback_dispatcher.py` | Gateway CLI timeout; default `10000` |
| `OPENCLAW_BIN` | no | `callback_dispatcher.py` | Explicit path to `openclaw`; otherwise auto-detected or `openclaw` |
| `DISPATCHER_LOG_LEVEL` | no | `callback_dispatcher.py` | Python logging level; default `INFO` |
| `OPENCLAW_CALLBACK_SPOOL_DIR` | no | `main.py` dashboard only | Legacy TUI counter directory; not the real dispatcher queue |
| `OPENCLAW_CALLBACK_SENT_DIR` | no | `main.py` dashboard only | Legacy TUI counter directory |
| `OPENCLAW_CALLBACK_FAILED_DIR` | no | `main.py` dashboard only | Legacy TUI counter directory |

Keyword files:

- `keywords.txt`: news keywords, one per line, `#` comments ignored
- `negative_keywords.txt`: exclusion keywords, one per line, `#` comments ignored

## Running locally

Long-running bot and dashboard:

```bash
source .venv/bin/activate
python main.py
```

One-shot callback producer and dispatcher:

```bash
python trigger_worker.py --once --entity-type vacancies
python trigger_worker.py --once --entity-type news
python callback_dispatcher.py --once
```

Dry-run modes:

```bash
python trigger_worker.py --once --dry-run
python callback_dispatcher.py --once --dry-run
```

CLI tools:

```bash
python mcp_server.py list
python mcp_server.py news
python mcp_server.py apply <vacancy_id> "cover letter"
python mcp_server.py ignore <vacancy_id>
python mcp_server.py delete <vacancy_id>
python mcp_server.py processed <vacancy_id>
python mcp_server.py mark_news <id1> <id2>
python mcp_server.py repost_news <news_id>
python mcp_server.py delete_news <news_id>
python mcp_server.py report "status text"
python mcp_server.py post "message text"
```

## Linux systemd

The `systemd/` directory contains templates using `/opt/parserUserBot` and `User=parseruserbot`. Edit them if your path or service account differs.

```bash
sudo useradd --system --home /opt/parserUserBot --shell /usr/sbin/nologin parseruserbot || true
sudo mkdir -p /opt
sudo cp -a "$PWD" /opt/parserUserBot
sudo chown -R parseruserbot:parseruserbot /opt/parserUserBot
sudo cp /opt/parserUserBot/systemd/*.service /opt/parserUserBot/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now parserUserBot-main.service
sudo systemctl enable --now parserUserBot-trigger.timer parserUserBot-dispatcher.timer
```

Status and logs:

```bash
systemctl status parserUserBot-main.service
systemctl list-timers 'parserUserBot-*'
journalctl -u parserUserBot-main.service -f
journalctl -u parserUserBot-trigger.service -f
journalctl -u parserUserBot-dispatcher.service -f
```

## macOS launchd

The `launchd/` plists are templates. Replace `/Users/your-user/parserUserBot` with your checkout path before loading.

```bash
mkdir -p logs ~/Library/LaunchAgents
sed "s#/Users/your-user/parserUserBot#$PWD#g" launchd/com.kisskin.parseruserbot.trigger.plist > ~/Library/LaunchAgents/com.kisskin.parseruserbot.trigger.plist
sed "s#/Users/your-user/parserUserBot#$PWD#g" launchd/com.kisskin.parseruserbot.dispatcher.plist > ~/Library/LaunchAgents/com.kisskin.parseruserbot.dispatcher.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.kisskin.parseruserbot.trigger.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.kisskin.parseruserbot.dispatcher.plist
launchctl kickstart -k "gui/$(id -u)/com.kisskin.parseruserbot.trigger"
launchctl kickstart -k "gui/$(id -u)/com.kisskin.parseruserbot.dispatcher"
```

Use `bin/start_parseruserbot.sh` on macOS if you want the helper script to validate `.env`, run one-shot checks, and load the plists.

## Dashboard

`main.py` starts a Rich terminal dashboard. It shows:

- total/pending vacancies
- total/unread news
- callback spool counters from legacy runtime directories
- systemd/launchd status for trigger/dispatcher labels where available
- `openclaw gateway status` result when `openclaw` is installed
- last action/error

There is no HTTP dashboard in the current codebase.

## MCP / agent integration

The real integration surface is `mcp_server.py`: a command-line tool whose commands map directly to async functions in the same file. External MCP-capable agents can expose these commands as tools, but this repository does not implement the MCP JSON-RPC protocol itself.

Callback flow:

1. `trigger_worker.py` selects unnotified `vacancies` or `news`
2. it builds a payload and fingerprint from item IDs
3. it upserts `callback_batches`
4. it inserts/upserts `callback_outbox` with `idempotency_key=parserUserBot:<entity_type>:<fingerprint>`
5. `callback_dispatcher.py` reads due `callback_outbox` rows
6. it calls `openclaw gateway call chat.send --json --params ...`
7. success marks outbox and batch as `sent`; failure records error and schedules retry

## Security and privacy

- Treat Telegram API credentials, phone numbers, Pyrogram session files, PostgreSQL URLs, gateway tokens, resumes, logs, and private chat content as sensitive.
- `.env`, `*.session`, logs, runtime files, PDFs, HTML resumes, and local DB files are gitignored.
- The code stores raw Telegram message text in PostgreSQL. Do not expose DB dumps publicly.
- `mcp_server.py apply` may send a real message and resume through the Telegram account once `main.py` processes the queued commands.
- `callback_dispatcher.py` sends callback message payloads to the configured OpenClaw gateway; verify the gateway destination before enabling timers.
- For public forks, keep only `.env.example` placeholders and template service files.

## Troubleshooting

| Symptom | Check |
|---|---|
| `asyncpg` connection error | Verify `DATABASE_URL`, PostgreSQL is running, and the DB user has permissions |
| Telegram login fails | Verify `API_ID`, `API_HASH`, `PHONE_NUMBER`; delete stale `my_account.session` only if you intend to re-login |
| No vacancies found | Check `keywords.txt`, `negative_keywords.txt`, and whether messages include both job context and `go/golang/java` |
| `apply` returns `No recruiter contact found` | The source message had no mention entity and no `from_user.username` fallback |
| Commands stay pending | Ensure `main.py` is running; `command_worker` polls `commands` every 5 seconds |
| Callback not sent | Run `python callback_dispatcher.py --once --dry-run`, verify `openclaw` path, `OPENCLAW_GATEWAY_URL`, and `OPENCLAW_GATEWAY_TOKEN` |
| systemd service fails | Check paths/user in service templates, `.env` readability, and `journalctl -u parserUserBot-main.service` |
| launchd job fails | Replace template paths, verify `.venv/bin/python`, inspect files in `logs/` |

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e '.[dev]'
ruff check .
python -m compileall -q .
python -m pytest -q
```

## Contributing

See `CONTRIBUTING.md`.

## Security reports

See `SECURITY.md`.

## License

MIT License. Copyright (c) 2026 Nazar (Kisskin-Mister).
