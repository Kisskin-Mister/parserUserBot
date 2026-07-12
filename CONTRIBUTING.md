# Contributing

Thanks for helping improve parserUserBot.

## Development setup

```bash
git clone https://github.com/Kisskin-Mister/parserUserBot.git
cd parserUserBot
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
pip install pytest ruff
cp .env.example .env
```

Use safe local values in `.env`. Do not commit real Telegram credentials, phone numbers, Pyrogram session files, PostgreSQL URLs with passwords, logs, resumes, or private message exports.

## Quality checks

Run these before opening a PR:

```bash
ruff check .
python -m compileall -q \
  -x '(^|/)(\.git|\.venv|\.venv-community|__pycache__|\.pytest_cache|\.ruff_cache)(/|$)' \
  .
python -m pytest -q
```

## Pull requests

1. Keep changes focused and small.
2. Update `README.md`, `ARCHITECTURE.md`, or `docs/` when behavior changes.
3. Add or update tests for code changes.
4. Use `.env.example` placeholders only; never use masked real values such as `abc***xyz`.
5. Explain user-visible changes and test results in the PR description.

## Code style

- Python 3.10+.
- Prefer explicit async boundaries and parameterized SQL.
- Avoid hardcoded paths except documented service templates.
- Keep CLI output JSON-compatible where existing commands already return JSON.

## Security and privacy

This project works with a real Telegram user account and stores raw message text. Treat all runtime data as private. If you discover a security issue, do not open a public issue with exploit details; follow `SECURITY.md`.
