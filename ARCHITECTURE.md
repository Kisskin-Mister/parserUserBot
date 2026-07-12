# Architecture

Detailed technical documentation for parserUserBot.

---

## Component Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        parserUserBot                                │
│                                                                     │
│  ┌──────────┐  ┌──────────────┐  ┌───────────┐  ┌──────────────┐  │
│  │ main.py  │  │  classifier  │  │ database  │  │  dashboard   │  │
│  │ (daemon) │──│    .py       │──│   .py     │──│    .py       │  │
│  └────┬─────┘  └──────────────┘  └─────┬─────┘  └──────────────┘  │
│       │                                │                            │
│       │         ┌──────────────────────┼──────────────────────┐    │
│       │         │                      │                      │    │
│  ┌────┴──────┐  │  ┌───────────────┐  │  ┌────────────────┐  │    │
│  │ message_  │  │  │ trigger_      │  │  │ callback_      │  │    │
│  │ policy.py │  │  │ worker.py     │  │  │ dispatcher.py  │  │    │
│  └───────────┘  │  └───────┬───────┘  │  └────────┬───────┘  │    │
│                  │          │          │           │           │    │
│                  │          └──────────┼───────────┘           │    │
│                  │                     │                       │    │
│                  │              ┌──────┴──────┐                │    │
│                  │              │ mcp_server  │                │    │
│                  │              │    .py      │                │    │
│                  │              └─────────────┘                │    │
│                  └────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
         │                                     │
         ▼                                     ▼
   ┌───────────┐                     ┌─────────────────┐
   │ Telegram  │                     │   PostgreSQL    │
   │  (Pyrogram)│                     │    Database     │
   └───────────┘                     └─────────────────┘
```

---

## Data Flow

### 1. Message Ingestion

```
Telegram Channel
       │
       ▼
   main.py (Pyrogram event handler)
       │
       ├── is_recruiter_private_message()?
       │   └── Yes → Forward to OWNER_USERNAME
       │
       ├── should_mark_chat_as_read()?
       │   └── Yes → Mark chat as read
       │
       ▼
   classify_post(text)
       │
       ├── Negative keyword match? → Skip (return None)
       │
       ├── Has job context + tech keyword?
       │   ├── "go"/"golang" → ("vacancy", "go")
       │   └── "java" → ("vacancy", "java")
       │
       ├── Has news keyword? → ("news", None)
       │
       └── No match → (None, None)
```

### 2. Vacancy Processing Pipeline

```
classify_post() → "vacancy"
       │
       ▼
   Extract recruiter @username from message entities
       │
       ▼
   db.add_vacancy(chat_id, message_id, chat_name, text, recruiter, tech)
       │  (ON CONFLICT chat_id, message_id DO NOTHING — deduplication)
       │
       ▼
   [Vacancy stored with status="pending"]
       │
       ▼ (trigger_worker polls every 30s)
   db.get_unnotified_vacancies()
       │  (WHERE status='pending' AND callback_notified_at IS NULL)
       │
       ▼
   Build fingerprint (SHA-256 of sorted item IDs)
       │
       ▼
   db.create_or_get_callback_batch()
       │  (idempotent — same fingerprint returns existing batch)
       │
       ▼
   db.enqueue_callback_outbox()
       │  (idempotency_key = "parserUserBot:{entity_type}:{fingerprint}")
       │
       ▼ (callback_dispatcher polls every 30s)
   db.get_due_callback_outbox()
       │  (WHERE status IN ('pending','failed') AND attempts < max_attempts)
       │
       ▼
   OpenClaw gateway call (via CLI subprocess)
       │
       ├── Success → mark_callback_outbox_sent()
       └── Failure → mark_callback_outbox_failed() (exponential backoff)
```

### 3. News Processing Pipeline

```
classify_post() → "news"
       │
       ▼
   Extract source_link from message entities
       │
       ▼
   db.add_news(chat_id, message_id, chat_name, text, source_link)
       │
       ▼
   [News stored with is_checked=FALSE]
       │
       ▼ (AI agent reads via mcp_server.py news)
   Agent classifies: relevant vs trash
       │
       ├── Relevant → mcp_server.py repost_news <id>
       │                  → db.add_command("repost_news", ...)
       │                  → main.py command_worker executes
       │
       └── Trash → mcp_server.py delete_news <id>
                      → db.delete_news(id)
```

---

## Database Schema

### `vacancies`

```sql
CREATE TABLE vacancies (
    id                  SERIAL PRIMARY KEY,
    chat_id             BIGINT,
    message_id          BIGINT,
    chat_name           TEXT,
    text                TEXT,
    recruiter_username  TEXT,
    status              TEXT DEFAULT 'pending',  -- pending | applied | ignored | processed
    tech                TEXT,                     -- "go" | "java" | NULL
    timestamp           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    callback_notified_at TIMESTAMP NULL,
    UNIQUE(chat_id, message_id)
);
```

### `news`

```sql
CREATE TABLE news (
    id                  SERIAL PRIMARY KEY,
    chat_id             BIGINT,
    message_id          BIGINT,
    chat_name           TEXT,
    text                TEXT,
    source_link         TEXT,
    repost_chat_id      BIGINT,
    repost_message_id   BIGINT,
    is_checked          BOOLEAN DEFAULT FALSE,
    timestamp           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    callback_notified_at TIMESTAMP NULL,
    UNIQUE(chat_id, message_id)
);
```

### `commands`

```sql
CREATE TABLE commands (
    id              SERIAL PRIMARY KEY,
    command_type    TEXT,        -- send_message | send_document | forward_message | repost_news
    target          TEXT,        -- @username or chat_id
    data            TEXT,        -- message text, file path, or JSON payload
    dedupe_key      TEXT,
    status          TEXT DEFAULT 'pending'  -- pending | completed | failed
);
-- UNIQUE INDEX on dedupe_key WHERE dedupe_key IS NOT NULL
```

### `recruiter_interactions`

```sql
CREATE TABLE recruiter_interactions (
    username            TEXT PRIMARY KEY,
    last_interaction    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### `recruiter_private_notifications`

```sql
CREATE TABLE recruiter_private_notifications (
    chat_id     BIGINT NOT NULL,
    message_id  BIGINT NOT NULL,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chat_id, message_id)
);
```

### `vacancy_outbox`

```sql
CREATE TABLE vacancy_outbox (
    id                  BIGSERIAL PRIMARY KEY,
    vacancy_id          BIGINT NOT NULL,
    recruiter_username  TEXT,
    command_type        TEXT NOT NULL,
    payload             TEXT,
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- UNIQUE INDEX on (vacancy_id, command_type)
```

### `callback_batches`

```sql
CREATE TABLE callback_batches (
    id              BIGSERIAL PRIMARY KEY,
    entity_type     TEXT NOT NULL,      -- "vacancies" | "news"
    fingerprint     TEXT NOT NULL,      -- SHA-256 of sorted item IDs
    payload         JSONB NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | sending | sent
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at         TIMESTAMP NULL,
    UNIQUE(entity_type, fingerprint)
);
```

### `callback_outbox`

```sql
CREATE TABLE callback_outbox (
    id                  BIGSERIAL PRIMARY KEY,
    entity_type         TEXT NOT NULL,
    batch_id            BIGINT REFERENCES callback_batches(id) ON DELETE SET NULL,
    session_key         TEXT NOT NULL,
    message             TEXT NOT NULL,
    idempotency_key     TEXT NOT NULL UNIQUE,
    status              TEXT NOT NULL DEFAULT 'pending',  -- pending | sending | sent | failed | dead
    attempts            INTEGER NOT NULL DEFAULT 0,
    max_attempts        INTEGER NOT NULL DEFAULT 10,
    next_attempt_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_error          TEXT,
    last_response       TEXT,
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at             TIMESTAMP NULL
);
```

### Entity Relationship

```
vacancies ─────┐
               ├──→ callback_batches ──→ callback_outbox
news ──────────┘

vacancies ──→ vacancy_outbox
vacancies ──→ recruiter_interactions
commands ──→ (executed by main.py command_worker)
recruiter_private_notifications
```

---

## Keyword Matching Algorithm

### Overview

The classifier uses **pre-compiled regex patterns** for fast matching:

```python
# Pattern compilation (at module load time)
def _compile_word_pattern(words: list[str]) -> re.Pattern:
    escaped = [re.escape(word) for word in words]
    return re.compile(r"(?<!\w)(?:" + "|".join(escaped) + r")(?!\w)", re.IGNORECASE)
```

The `(?<!\w)...(?!\w)` word boundary pattern prevents partial matches (e.g., "go" won't match "google").

### Classification Logic

```
Input: text
  │
  ▼
text_lower = text.lower()
  │
  ├── NEGATIVE_PATTERN.search(text_lower)?
  │   └── Yes → return (None, None)     # Excluded
  │
  ├── VACANCY_CONTEXT_PATTERN.search(text_lower)?
  │   │   AND
  │   ├── TECH_PATTERN.search(text_lower)?
  │   │   │
  │   │   ├── GO_PATTERN.search(text_lower)?
  │   │   │   └── Yes → return ("vacancy", "go")
  │   │   │
  │   │   └── JAVA_PATTERN.search(text_lower)?
  │   │       └── Yes → return ("vacancy", "java")
  │   │
  │   └── No tech match → fall through
  │
  ├── NEWS_PATTERN.search(text_lower)?
  │   └── Yes → return ("news", None)
  │
  └── No match → return (None, None)
```

### Keyword Lists

**Vacancy context keywords** (built-in):
`вакансия`, `ищем`, `job`, `hiring`, `стек`, `зп`, `salary`, `remote`, `удаленка`

**Tech keywords** (built-in):
`go`, `golang`, `java`

**News keywords** (`keywords.txt`):
AI, neural networks, ChatGPT, GPT, LLM, Stable Diffusion, Midjourney, etc.

**Negative keywords** (`negative_keywords.txt`):
Excludes spam: реклама, Яндекс, МТС, ВТБ, Т-Банк, Сбер, etc.

---

## MCP Server Tools/Endpoints

`mcp_server.py` provides both async functions and CLI commands:

### Async Functions (for programmatic use)

| Function | Description | Returns |
|---|---|---|
| `get_vacancies()` | List pending vacancies | `list[dict]` |
| `get_news()` | List unread news | `list[dict]` |
| `apply_to_vacancy(id, text)` | Send application + resume | `dict` |
| `ignore_vacancy(id)` | Mark vacancy as ignored | `dict` |
| `delete_vacancy(id)` | Remove vacancy from DB | `dict` |
| `mark_vacancy_processed(id)` | Mark as processed | `dict` |
| `mark_news_read(ids)` | Mark news as checked | `dict` |
| `repost_news(id)` | Queue news repost | `dict` |
| `delete_news_item(id)` | Remove news from DB | `dict` |
| `send_status_report(text)` | Notify owner via DM | `dict` |
| `post_to_channel(text)` | Post to LOG_CHAT_ID | `dict` |

### CLI Commands

```bash
python mcp_server.py list                          # → get_vacancies()
python mcp_server.py news                          # → get_news()
python mcp_server.py apply <id> "text"             # → apply_to_vacancy()
python mcp_server.py ignore <id>                   # → ignore_vacancy()
python mcp_server.py delete <id>                   # → delete_vacancy()
python mcp_server.py processed <id>                # → mark_vacancy_processed()
python mcp_server.py mark_news <id1> <id2> ...     # → mark_news_read()
python mcp_server.py repost_news <id>              # → repost_news()
python mcp_server.py delete_news <id>              # → delete_news_item()
python mcp_server.py report "text"                 # → send_status_report()
python mcp_server.py post "text"                   # → post_to_channel()
```

### Command Execution Flow

```
mcp_server.py apply 7 "Hi, I'm interested..."
       │
       ▼
   db.add_command("send_message", "@recruiter", "Hi, I'm interested...")
   db.add_command("send_document", "@recruiter", "resumes/resume_main.pdf")
   db.add_vacancy_outbox_entry(7, "@recruiter", ...)
   db.update_vacancy_status(7, "applied")
       │
       ▼ (main.py command_worker polls every 5s)
   db.get_pending_commands()
       │
       ▼
   client.send_message("@recruiter", "Hi, I'm interested...")
   client.send_document("@recruiter", "resumes/resume_main.pdf")
       │
       ▼
   db.mark_command_done(id, "completed")
```

---

## Graceful Shutdown

`main.py` registers SIGTERM and SIGINT handlers that:

1. Cancel all background tasks (dashboard, command_worker, sync_chats)
2. Stop the Pyrogram client
3. Close the database connection pool

This ensures clean shutdown when systemd sends SIGTERM on `systemctl stop`.

---

## Retry and Backoff Strategy

The callback outbox uses exponential backoff:

```python
delay_minutes = min(max(1, attempts), 10)
next_attempt_at = CURRENT_TIMESTAMP + delay_minutes * interval
```

- Attempt 1: retry after 1 minute
- Attempt 2: retry after 2 minutes
- ...
- Attempt 10: retry after 10 minutes (then marked as `dead`)
