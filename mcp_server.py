import sys
import asyncio
import json
import hashlib
from database import Database
import os
from dotenv import load_dotenv

load_dotenv()
db = Database()

def make_command_dedupe_key(command, target, payload):
    raw = f"{command}|{target}|{payload}".encode("utf-8", errors="ignore")
    return hashlib.sha256(raw).hexdigest()

async def get_vacancies():
    await db.init()
    return await db.get_pending_vacancies()

async def get_news():
    await db.init()
    rows = await db.get_unread_news()
    for r in rows:
        if 'timestamp' in r and r['timestamp']:
            r['timestamp'] = r['timestamp'].isoformat()
    return rows

async def mark_news_read(news_ids):
    await db.init()
    await db.mark_news_as_checked(news_ids)
    return {"status": "success"}

async def apply_to_vacancy(vacancy_id, message_text):
    await db.init()
    v = await db.get_vacancy_by_id(vacancy_id)
    if not v:
        return {"error": "Vacancy not found"}

    if v.get("status") == "applied" or await db.has_vacancy_outbox_entry(vacancy_id):
        return {"error": "Vacancy already applied", "vacancy_id": vacancy_id}
    
    recruiter = v["recruiter_username"]
    if not recruiter:
        return {"error": "No recruiter contact found for this vacancy"}
    
    await db.add_command(
        "send_message",
        recruiter,
        message_text,
    )
    await db.add_vacancy_outbox_entry(vacancy_id, recruiter, "send_message", message_text)

    resume_path = os.getenv("RESUME_PATH")
    if resume_path and os.path.exists(resume_path):
        await db.add_command(
            "send_document",
            recruiter,
            resume_path,
        )
        await db.add_vacancy_outbox_entry(vacancy_id, recruiter, "send_document", resume_path)
    
    await db.update_vacancy_status(vacancy_id, 'applied')
    await db.add_interaction(recruiter)
    return {"status": "success", "recruiter": recruiter}

async def ignore_vacancy(vacancy_id):
    await db.init()
    await db.update_vacancy_status(vacancy_id, 'ignored')
    return {"status": "ignored"}

async def delete_vacancy(vacancy_id):
    await db.init()
    await db.delete_vacancy(vacancy_id)
    return {"status": "deleted"}

async def mark_vacancy_processed(vacancy_id):
    await db.init()
    await db.update_vacancy_status(vacancy_id, 'processed')
    return {"status": "processed"}

async def send_status_report(text):
    await db.init()
    owner_username = os.getenv("OWNER_USERNAME", "zogpower")
    await db.add_command("send_message", owner_username, text)
    return {"status": "report_sent"}

async def post_to_channel(text):
    await db.init()
    channel_id = os.getenv("LOG_CHAT_ID")
    if not channel_id:
        return {"error": "LOG_CHAT_ID not set"}
    await db.add_command(
        "send_message",
        channel_id,
        text,
    )
    return {"status": "posted_to_channel"}

async def repost_news(news_id):
    await db.init()
    target_chat_id = os.getenv("NEWS_CHAT_ID")
    if not target_chat_id:
        return {"error": "NEWS_CHAT_ID not set"}

    news = await db.get_news_by_id(news_id)
    if not news:
        return {"error": "News not found"}

    source_chat_clean = str(news["chat_id"]).replace("-100", "")
    source_link = f"https://t.me/c/{source_chat_clean}/{news['message_id']}"
    raw_text = (news.get("text") or "").strip()
    summary = raw_text.splitlines()[0][:240] if raw_text else "новость"

    post_text = (
        f"📰 {summary}\n\n"
        f"Источник: {source_link}"
    )

    payload = {
        "news_id": int(news_id),
        "text": post_text
    }
    payload_json = json.dumps(payload, ensure_ascii=False)
    await db.add_command(
        "repost_news",
        str(target_chat_id),
        payload_json,
    )
    return {"status": "repost_queued", "target_chat_id": str(target_chat_id), "source_news_id": news_id, "source_link": source_link}

async def delete_news_item(news_id):
    await db.init()
    await db.delete_news(news_id)
    return {"status": "deleted_news"}

if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(1)

    def _dump(payload):
        print(json.dumps(payload, ensure_ascii=False, default=str))

    cmd = sys.argv[1]
    if cmd == "list":
        _dump(asyncio.run(get_vacancies()))
    elif cmd == "news":
        _dump(asyncio.run(get_news()))
    elif cmd == "report":
        msg = sys.argv[2]
        _dump(asyncio.run(send_status_report(msg)))
    elif cmd == "mark_news":
        ids = [int(x) for x in sys.argv[2:]]
        _dump(asyncio.run(mark_news_read(ids)))
    elif cmd == "apply":
        vid = int(sys.argv[2])
        msg = sys.argv[3]
        _dump(asyncio.run(apply_to_vacancy(vid, msg)))
    elif cmd == "ignore":
        vid = int(sys.argv[2])
        _dump(asyncio.run(ignore_vacancy(vid)))
    elif cmd == "delete":
        vid = int(sys.argv[2])
        _dump(asyncio.run(delete_vacancy(vid)))
    elif cmd == "processed":
        vid = int(sys.argv[2])
        _dump(asyncio.run(mark_vacancy_processed(vid)))
    elif cmd == "post":
        msg = sys.argv[2]
        _dump(asyncio.run(post_to_channel(msg)))
    elif cmd == "repost_news":
        nid = int(sys.argv[2])
        _dump(asyncio.run(repost_news(nid)))
    elif cmd == "forward_news":
        nid = int(sys.argv[2])
        _dump(asyncio.run(repost_news(nid)))
    elif cmd == "delete_news":
        nid = int(sys.argv[2])
        _dump(asyncio.run(delete_news_item(nid)))
