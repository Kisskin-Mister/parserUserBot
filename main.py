import os
import asyncio
import subprocess
from pathlib import Path

from pyrogram import Client, filters, enums
from dotenv import load_dotenv
from rich.live import Live

from classifier import classify_post
from database import Database
from dashboard import Dashboard
from message_policy import is_recruiter_private_message, should_mark_chat_as_read

load_dotenv()

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
LOG_CHAT_ID = os.getenv("LOG_CHAT_ID")
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "zogpower")
PHONE_NUMBER = os.getenv("PHONE_NUMBER")
CALLBACK_SPOOL_DIR = Path(os.getenv("OPENCLAW_CALLBACK_SPOOL_DIR", "runtime/callback_spool"))
CALLBACK_SENT_DIR = Path(os.getenv("OPENCLAW_CALLBACK_SENT_DIR", "runtime/callback_sent"))
CALLBACK_FAILED_DIR = Path(os.getenv("OPENCLAW_CALLBACK_FAILED_DIR", "runtime/callback_failed"))

app = Client("my_account", api_id=API_ID, api_hash=API_HASH, phone_number=PHONE_NUMBER)
db = Database()
dash = Dashboard()


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
    result = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        text=True,
    )
    return "loaded" if result.returncode == 0 else "not loaded"


def get_gateway_status() -> str:
    result = subprocess.run(["openclaw", "gateway", "status"], capture_output=True, text=True)
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode == 0 and "RPC probe: ok" in output:
        return "ok"
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

        post_type, tech = await classify_post(text)

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


async def catch_up_unread(client):
    dash.update_stats(status="Syncing unread...")
    async for dialog in client.get_dialogs():
        if dialog.unread_messages_count > 0:
            messages = [
                message async for message in client.get_chat_history(dialog.chat.id, limit=dialog.unread_messages_count)
            ]
            non_recruiter_messages = [message for message in messages if not is_recruiter_private_message(message)]
            for message in non_recruiter_messages:
                await process_message(client, message, is_initial=True)

            should_read_dialog = any(should_mark_chat_as_read(message) for message in non_recruiter_messages)
            if should_read_dialog:
                await client.read_chat_history(dialog.chat.id)


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
                trigger_job=get_job_status("com.kisskin.parseruserbot.trigger"),
                dispatcher_job=get_job_status("com.kisskin.parseruserbot.dispatcher"),
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
                        payload = {}
                        try:
                            import json
                            payload = json.loads(cmd["data"] or "{}")
                        except Exception:
                            payload = {}

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
                        payload = {}
                        try:
                            import json
                            payload = json.loads(cmd["data"] or "{}")
                        except Exception:
                            payload = {}

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
            async for dialog in client.get_dialogs():
                if dialog.unread_messages_count > 0:
                    messages = [
                        message async for message in client.get_chat_history(dialog.chat.id, limit=dialog.unread_messages_count)
                    ]
                    for message in messages:
                        await process_message(client, message, is_initial=True)

                    should_read_dialog = any(should_mark_chat_as_read(message) for message in messages)
                    if should_read_dialog:
                        await client.read_chat_history(dialog.chat.id)
            dash.update_stats(last_action="Sync completed ⚡")
        except Exception as exc:
            dash.update_stats(last_error=str(exc)[:80])
        await asyncio.sleep(60)


async def run_bot():
    await db.init()
    dash.update_stats(status="Connecting...")
    await app.start()
    dash.update_stats(status="🟢 Online")

    asyncio.create_task(dashboard_task())
    asyncio.create_task(command_worker(app))
    asyncio.create_task(sync_chats_task(app))

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
