# Security Policy

## Supported versions

Security fixes are handled on the `main` branch. Public releases are not currently versioned.

## Reporting a vulnerability

Please report vulnerabilities privately to the maintainer instead of opening a public issue with exploit details.

Include:

- affected component or file
- impact and reproduction steps
- whether real Telegram credentials, session files, database dumps, resumes, or private chat content may be exposed
- suggested mitigation if known

The maintainer will acknowledge the report as soon as practical, investigate, and publish a fix or advisory when appropriate.

## Sensitive data

Never commit or disclose:

- `.env` files
- Telegram `API_ID`, `API_HASH`, phone numbers, login codes, or 2FA passwords
- Pyrogram `*.session` / `*.session-journal` files
- PostgreSQL URLs with real usernames/passwords/hosts
- OpenClaw gateway tokens
- logs containing private message text
- resumes, PDFs, generated HTML resumes, or personal documents

If any credential or session file is exposed, rotate/revoke it immediately. For Telegram user sessions, terminate the compromised session in Telegram settings and regenerate local session files.

## Runtime risk notes

- This is a Telegram user-account client, not a Telegram Bot API bot.
- `mcp_server.py apply` queues real outbound messages/resume sends when `main.py` processes pending commands.
- `callback_dispatcher.py` sends callback payloads to the configured gateway. Verify the gateway URL/token before enabling timers.
- The PostgreSQL database stores raw Telegram message text; do not publish dumps or logs.
