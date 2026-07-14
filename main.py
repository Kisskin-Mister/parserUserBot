import asyncio
import json
import logging
import os
import platform
import signal
import subprocess
from pathlib import Path

from pyrogram import Client, filters, enums
from dotenv import load_dotenv
from rich.live import Live

from classifier import classify_post
from database import Database
from dashboard import Dashboard
from message_policy import is_recruiter_private_message, should_mark_chat_as_read
from parser_engine import AIClassifier, match_parser_config, normalize_chat_ids, should_skip_chat

load_dotenv()

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
LOG_CHAT_ID = os.getenv("LOG_CHAT_ID")
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "zogpower")
PHONE_NUMBER = os.getenv("PHONE_NUMBER")
CALLBACK_SPOOL_DIR = Path(os.getenv("OPENCLAW_CALLBACK_SPOOL_DIR", "runtime/callback_spool"))
CALLBACK_SENT_DIR = Path(os.getenv("OPENCLAW_CALLBACK_SENT_DIR", "runtime/callback_sent"))
CALLBACK_FAILED_DIR = Path(os.getenv("OPENCLAW_CALLBACK_FAILED_DIR", "runtime/callback_failed"))
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
FORWARD_MODE = os.getenv("FORWARD_MODE", "forward")
IGNORE_PRIVATE_CHATS = os.getenv("IGNORE_PRIVATE_CHATS", "true").lower() in {"1", "true", "yes", "on"}
EXCLUDED_CHAT_IDS = normalize_chat_ids(os.getenv("EXCLUDED_CHAT_IDS", ""))

SESSION_NAME = os.getenv("SESSION_NAME", "my_account")
app = Client(SESSION_NAME, api_id=API_ID, api_hash=API_HASH, phone_number=PHONE_NUMBER)
db = Database()
dash = Dashboard()
ai_classifier = AIClassifier(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY, model=OPENAI_MODEL)

logger = logging.getLogger(__name__)

# Track background tasks for graceful shutdown
_background_tasks: list[asyncio.Task] = []


async def get_db_stats():
    async with db.pool.acquire() as conn:
        v_total = await conn.fetchval("SELECT count(*) FROM vacancies")
        v_pending = await conn.fetchval("SELECT count(*) FROM vacancies WHERE status = 'pending'")
        n_total = await conn.fetchval("SELECT count(*) FROM news")
        n_unread = await conn.fetchval("SELECT count(*) FROM news WHERE is_checked = FALSE")
        return v_total, v_pending, n_total, n_unread


def format_last_outbox(last_row: dict | None) -> str:
    if not last_row:
        return "None"
    recruiter = last_row.get("recruiter_username") or "unknown"
    command_type = last_row.get("command_type") or "unknown"
    vacancy_id = last_row.get("vacancy_id") or "?"
    created_at = last_row.get("created_at")
    created_text = created_at.strftime("%m-%d %H:%M") if created_at else "unknown"
    return f"#{vacancy_id} {command_type} → {recruiter} @ {created_text}"


def get_job_status(label: str) -> str:
    system = platform.system()
    if system == "Darwin":
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
        )
        return "loaded" if result.returncode == 0 else "not loaded"
    elif system == "Linux":
        result = subprocess.run(
            ["systemctl", "is-active", label],
            capture_output=True,
            text=True,
        )
        status = (result.stdout or "").strip()
        return status if status else "inactive"
    return "unknown"


def get_gateway_status() -> str:
    try:
        result = subprocess.run(["openclaw", "gateway", "status"], capture_output=True, text=True, timeout=5)
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode == 0 and "RPC probe: ok" in output:
            return "ok"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "down"


def count_files(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    return len(list(path.glob("*.json")))


async def process_message(client, message, is_initial=False):
    try:
        chat_id = message.chat.id
        chat_title = message.chat.title or message.chat.first_name or "Unknown"
        text = message.text or message.caption or ""
        if not text or text == "[No Text]":
            return
        if str(chat_id) == LOG_CHAT_ID:
            return
        is_private_chat = is_recruiter_private_message(message)
        if IGNORE_PRIVATE_CHATS and is_private_chat:
            return
        if str(chat_id) in EXCLUDED_CHAT_IDS:
            return

        handled_by_unified_parser = False
        for parser in await db.list_parser_configs(active_only=True):
            if should_skip_chat(chat_id, parser, is_private=is_private_chat):
                continue

            first_seen = await db.try_mark_message_processing(parser["id"], chat_id, message.id)
            if not first_seen:
                continue

            result = await match_parser_config(text, parser, ai_classifier)
            target_message_id = None
            target_chat_id = parser["target_chat_id"]
            if result.matched:
                try:
                    if FORWARD_MODE == "copy":
                        sent = await client.send_message(target_chat_id, text)
                    else:
                        sent = await client.forward_messages(
                            chat_id=target_chat_id,
                            from_chat_id=chat_id,
                            message_ids=[message.id],
                        )
                    target_message_id = getattr(sent, "id", None)
                    dash.update_stats(last_action=f"Parser {parser['id']} matched")
                except Exception as exc:
                    await db.mark_processed_message(
                        parser["id"],
                        chat_id,
                        message.id,
                        True,
                        target_chat_id=target_chat_id,
                        target_message_id=None,
                        error=str(exc),
                    )
                    handled_by_unified_parser = True
                    continue

            await db.mark_processed_message(
                parser["id"],
                chat_id,
                message.id,
                result.matched,
                target_chat_id=target_chat_id if result.matched else None,
                target_message_id=target_message_id,
                error=result.error,
            )
            handled_by_unified_parser = True

        # Legacy HR/news behavior remains as a compatibility fallback while users
        # migrate to API-managed parser_configs. If at least one unified parser
        # processed the message, do not also write old vacancy/news rows.
        if handled_by_unified_parser:
            if should_mark_chat_as_read(message):
                await client.read_chat_history(chat_id)
            return

        post_type, tech = classify_post(text)

        if post_type == "vacancy":
            recruiter = None
            if message.entities:
                for entity in message.entities:
                    if entity.type == enums.MessageEntityType.MENTION:
                        recruiter = text[entity.offset:entity.offset + entity.length]
                        break
            if not recruiter and message.from_user and message.from_user.username:
                recruiter = f"@{message.from_user.username}"

            await db.add_vacancy(chat_id, message.id, chat_title, text, recruiter, tech)
            dash.update_stats(last_action=f"Found {tech} vacancy")

        elif post_type == "news":
            source_link = None
            if message.entities:
                for entity in message.entities:
                    if entity.type in [enums.MessageEntityType.URL, enums.MessageEntityType.TEXT_LINK]:
                        source_link = getattr(entity, "url", text[entity.offset:entity.offset + entity.length])
                        break
            await db.add_news(chat_id, message.id, chat_title, text, source_link)
            dash.update_stats(last_action=f"Found news in {chat_title[:15]}")

        if should_mark_chat_as_read(message):
            await client.read_chat_history(chat_id)

        if is_recruiter_private_message(message):
            already_notified = await db.recruiter_private_notification_exists(chat_id, message.id)
            if not already_notified:
                await client.send_message(OWNER_USERNAME, "⚡️ рекрутер написал в лс:")
                await message.forward(OWNER_USERNAME)
                await db.add_recruiter_private_notification(chat_id, message.id)

    except Exception as exc:
        dash.update_stats(last_action=f"Error: {str(exc)[:20]}", last_error=str(exc)[:80])


@app.on_message(filters.text & ~filters.me)
async def message_handler(client, message):
    await process_message(client, message)


async def _process_unread_dialogs(client, mark_as_read=True):
    """Common logic for catch_up_unread and sync_chats_task."""
    async for dialog in client.get_dialogs():
        if dialog.unread_messages_count > 0:
            messages = [
                message async for message in client.get_chat_history(dialog.chat.id, limit=dialog.unread_messages_count)
            ]
            non_recruiter_messages = [m for m in messages if not is_recruiter_private_message(m)]
            for message in non_recruiter_messages:
                await process_message(client, message, is_initial=True)

            if mark_as_read:
                should_read_dialog = any(should_mark_chat_as_read(m) for m in non_recruiter_messages)
                if should_read_dialog:
                    await client.read_chat_history(dialog.chat.id)


async def catch_up_unread(client):
    dash.update_stats(status="Syncing unread...")
    await _process_unread_dialogs(client, mark_as_read=True)


async def dashboard_task():
    while True:
        try:
            v_total, v_pending, n_total, n_unread = await get_db_stats()
            dash.update_stats(
                vacancies_total=v_total,
                vacancies_pending=v_pending,
                news_total=n_total,
                news_unread=n_unread,
                spool_pending=count_files(CALLBACK_SPOOL_DIR),
                spool_sent=count_files(CALLBACK_SENT_DIR),
                spool_failed=count_files(CALLBACK_FAILED_DIR),
                trigger_job=get_job_status("parserUserBot-trigger"),
                dispatcher_job=get_job_status("parserUserBot-dispatcher"),
                gateway_status=get_gateway_status(),
            )
        except Exception as exc:
            dash.update_stats(last_error=str(exc)[:80])
        await asyncio.sleep(5)


async def command_worker(client):
    while True:
        try:
            pending_commands = await db.get_pending_commands()
            for cmd in pending_commands:
                target = cmd["target"].replace("@", "")
                try:
                    if cmd["command_type"] == "send_message":
                        await client.send_message(target, cmd["data"])
                    elif cmd["command_type"] == "send_document":
                        await client.send_document(target, cmd["data"])
                    elif cmd["command_type"] == "forward_message":
                        payload = json.loads(cmd["data"] or "{}")

                        from_chat_id = payload.get("from_chat_id")
                        message_id = payload.get("message_id")
                        if not from_chat_id or not message_id:
                            raise ValueError("forward_message payload must include from_chat_id and message_id")

                        await client.forward_messages(
                            chat_id=target,
                            from_chat_id=str(from_chat_id),
                            message_ids=[int(message_id)],
                        )
                    elif cmd["command_type"] == "repost_news":
                        payload = json.loads(cmd["data"] or "{}")

                        news_id = payload.get("news_id")
                        text = payload.get("text")
                        if not news_id or not text:
                            raise ValueError("repost_news payload must include news_id and text")

                        posted = await client.send_message(target, text)
                        await db.set_news_repost(int(news_id), int(target), int(posted.id))
                    else:
                        raise ValueError(f"unknown command_type: {cmd['command_type']}")

                    await db.mark_command_done(cmd["id"])
                    dash.update_stats(last_action=f"Applied to {target}")
                except Exception as exc:
                    await db.mark_command_done(cmd["id"], status="failed")
                    dash.update_stats(last_error=str(exc)[:80])
            await asyncio.sleep(5)
        except Exception as exc:
            dash.update_stats(last_error=str(exc)[:80])
            await asyncio.sleep(10)


async def sync_chats_task(client):
    while True:
        try:
            await _process_unread_dialogs(client, mark_as_read=True)
            dash.update_stats(last_action="Sync completed ⚡")
        except Exception as exc:
            dash.update_stats(last_error=str(exc)[:80])
        await asyncio.sleep(60)


async def _shutdown(signame: str):
    """Graceful shutdown: cancel background tasks, stop app, close DB pool."""
    logger.info("Received %s — shutting down gracefully", signame)
    for task in _background_tasks:
        task.cancel()
    if app.is_connected:
        await app.stop()
    if db.pool:
        await db.pool.close()
    logger.info("Shutdown complete")


async def run_bot():
    await db.init()
    dash.update_stats(status="Connecting...")
    await app.start()
    dash.update_stats(status="🟢 Online")

    # Register signal handlers for graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signame = signal.Signals(sig).name
        loop.add_signal_handler(sig, lambda s=signame: asyncio.create_task(_shutdown(s)))

    _background_tasks.append(asyncio.create_task(dashboard_task()))
    _background_tasks.append(asyncio.create_task(command_worker(app)))
    _background_tasks.append(asyncio.create_task(sync_chats_task(app)))

    await catch_up_unread(app)

    with Live(dash.generate_layout(), refresh_per_second=1) as live:
        while True:
            live.update(dash.generate_layout())
            await asyncio.sleep(1)


if __name__ == "__main__":
    try:
        app.run(run_bot())
    except KeyboardInterrupt:
        pass
