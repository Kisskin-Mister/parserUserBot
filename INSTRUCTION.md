# OpenClaw Integration Guide

## Project Context
- **Root:** `/Users/kisskin/PycharmProjects/parserUserBot`
- **Main Agent:** Codex (Full access)
- **Sentinel Agent:** Qwen (LM Studio @ localhost:1234)

## Available Tools (via Python CLI)
- `python3 mcp_server.py list` -> Check for new vacancies (Pending status)
- `python3 mcp_server.py news` -> Check for unread IT news
- `python3 mcp_server.py report "<msg>"` -> Notify @zogpower via Telegram (Direct)
- `python3 mcp_server.py post "<msg>"` -> Post formatted vacancy to channel (LOG_CHAT_ID)
- `python3 mcp_server.py apply <id> "<letter>"` -> Send application to recruiter
- `python3 mcp_server.py delete <id>` -> Remove trash/duplicate/irrelevant from DB
- `python3 mcp_server.py processed <id>` -> Mark vacancy as handled
- `python3 mcp_server.py mark_news <id1> <id2>` -> Mark news as checked

## Workflow for OpenClaw
1. **Cron Trigger:** Every 30 mins OpenClaw starts a session.
2. **Sentinel Check:** Qwen runs `list` and `news`.
3. **Handover:** If data exists, Qwen signals Codex.
4. **Execution:** Codex analyzes text using `JOB_HUNTER_SKILL.md` or `IT_CURATOR_SKILL.md`.
5. **Report/Clean:** Codex posts good stuff, deletes trash, and updates DB statuses.

## Communication Style (Mandatory for Codex)
- lowercase only
- zoomer/millennial balance
- `-` instead of long dashes
- no periods at the end of lines
- new line for each sentence
