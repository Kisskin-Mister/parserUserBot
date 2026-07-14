# parserUserBot

Universal Telegram **user-account** parser and forwarder.

It reads chats/channels available to your Telegram account and forwards matching posts to target chats. It is not a Bot API bot: it uses a Telegram user session through Pyrogram.

## Features

- Multiple parser configs managed by REST API
- **Keyword mode**: keywords + negative keywords, `any` / `all` matching
- **AI mode**: OpenAI-compatible API returns strict `true` / `false`
- Per-parser source chats and target chat
- Deduplication per parser/source/message
- PostgreSQL persistence
- Docker Compose deployment
- Safe `.env.example` with placeholders only

## Modes

### 1. Keyword mode

A message matches when:

1. it comes from one of `source_chat_ids`
2. no `negative_keywords` match
3. `keywords` match by `policy`:
   - `any`: at least one keyword
   - `all`: every keyword

Example config body:

```json
{
  "name": "Remote backend jobs",
  "enabled": true,
  "mode": "keyword",
  "source_chat_ids": [-1001111111111],
  "target_chat_id": -1002222222222,
  "config": {
    "keywords": ["remote", "backend", "python"],
    "negative_keywords": ["internship", "course", "casino"],
    "policy": "any",
    "case_sensitive": false,
    "regex": false
  }
}
```

### 2. AI mode

A message is sent to an OpenAI-compatible chat-completions API. The model must return exactly one lowercase word: `true` or `false`.

Default system prompt:

```text
You are a strict binary classifier for Telegram posts. Your task is to decide whether the provided Telegram post matches the user's selection criteria. Rules: Return exactly one lowercase word: true or false. Do not explain your answer. Do not add punctuation, markdown, JSON, quotes, or extra text. Return true only if the post clearly matches the user's criteria. Return false if the post is irrelevant, ambiguous, promotional spam, unrelated, or there is not enough information. Negative constraints in the user's criteria always override positive signals. Classify only the provided post. Do not infer missing facts.
```

Example AI parser:

```json
{
  "name": "AI selected vacancies",
  "enabled": true,
  "mode": "ai",
  "source_chat_ids": [-1001111111111],
  "target_chat_id": -1002222222222,
  "config": {
    "user_prompt": "Return true only for remote backend developer jobs in Java, Go, Python or Rust. Return false for internships, frontend-only roles, courses, gambling, unpaid work, and relocation-only jobs.",
    "excluded_chat_ids": [-1003333333333],
    "ignore_private": true
  }
}
```

## Quick start with Docker Compose

```bash
git clone https://github.com/Kisskin-Mister/parserUserBot.git
cd parserUserBot
cp .env.example .env
# edit .env: API_ID, API_HASH, PHONE_NUMBER, DATABASE_URL, ADMIN_TOKEN

docker compose up -d --build
```

First Telegram login needs an interactive Pyrogram session. The simplest way:

```bash
docker compose run --rm worker python main.py
```

Enter the Telegram code and 2FA password if requested. The session is stored in the Docker volume `telegram_session`.

After login:

```bash
docker compose up -d
curl http://localhost:8000/health
```

## API

All mutating endpoints require your `.env` `ADMIN_TOKEN`:

```bash
-H "Authorization: Bearer <your-admin-token>"
```

### Health

```bash
curl http://localhost:8000/health
```

### Create parser

```bash
curl -X POST http://localhost:8000/parsers \
  -H "Authorization: Bearer <your-admin-token>" \
  -H "Content-Type: application/json" \
  --data @parser.json
```

### List parsers

```bash
curl http://localhost:8000/parsers
```

### Enable / disable

```bash
curl -X POST http://localhost:8000/parsers/1/disable \
  -H "Authorization: Bearer <your-admin-token>"

curl -X POST http://localhost:8000/parsers/1/enable \
  -H "Authorization: Bearer <your-admin-token>"
```

### Test a parser on text

```bash
curl -X POST http://localhost:8000/parsers/1/test-message \
  -H "Content-Type: application/json" \
  --data '{"message":"Remote Go backend vacancy, salary in USD"}'
```

### Events

```bash
curl http://localhost:8000/events?limit=50
```

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt pytest ruff
python -m pytest -q
ruff check .
python -m compileall -q .
```

## Security

- Never commit `.env`, Telegram `*.session` files, logs, resumes, or API keys.
- Protect `ADMIN_TOKEN`; do not expose the API publicly without reverse-proxy auth/TLS.
- Treat the Telegram session volume as sensitive: it is your Telegram account session.
- AI mode sends post text to the configured model provider.

## Notes

The repository still contains some legacy modules for the previous vacancy/news workflow. New deployments should use API-managed `parser_configs` and the universal keyword/AI engine.
