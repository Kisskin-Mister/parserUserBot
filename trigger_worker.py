import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
from typing import Any

from dotenv import load_dotenv

from database import Database

load_dotenv()

DEFAULT_SESSION_KEY = "agent:main:main"
DEFAULT_ENTITY_TYPE = "vacancies"
DEFAULT_LIMIT = 50
DEFAULT_LOG_LEVEL = "INFO"


def setup_logging() -> None:
    level_name = os.getenv("TRIGGER_LOG_LEVEL", DEFAULT_LOG_LEVEL).upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s | %(levelname)s | %(message)s")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DB marker callback outbox producer for parserUserBot")
    parser.add_argument("--once", action="store_true", help="Run one iteration and exit")
    parser.add_argument("--dry-run", action="store_true", help="Do not enqueue callback, only log payload")
    parser.add_argument(
        "--entity-type",
        choices=["vacancies", "news"],
        default=os.getenv("TRIGGER_ENTITY_TYPE", DEFAULT_ENTITY_TYPE),
        help="Which entity stream to inspect",
    )
    return parser.parse_args()


def sanitize_item(item: dict[str, Any]) -> dict[str, Any]:
    data = dict(item)
    if data.get("timestamp") is not None:
        data["timestamp"] = data["timestamp"].isoformat()
    text = (data.get("text") or "").strip()
    if len(text) > 1200:
        data["text"] = text[:1200] + "…"
    return data


def build_fingerprint(entity_type: str, items: list[dict[str, Any]]) -> str:
    normalized = [{"id": int(item["id"])} for item in items]
    raw = json.dumps({"entity_type": entity_type, "items": normalized}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_payload(entity_type: str, items: list[dict[str, Any]], fingerprint: str) -> dict[str, Any]:
    sanitized_items = [sanitize_item(item) for item in items]
    return {
        "source": "parserUserBot.trigger_worker",
        "entity_type": entity_type,
        "count": len(sanitized_items),
        "fingerprint": fingerprint,
        "items": sanitized_items,
    }


def build_message(payload: dict[str, Any]) -> str:
    return "[SCRIPT_CALLBACK] " + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


async def fetch_candidates(db: Database, entity_type: str, limit: int) -> list[dict[str, Any]]:
    if entity_type == "vacancies":
        return await db.get_unnotified_vacancies(limit=limit)
    if entity_type == "news":
        return await db.get_unnotified_news(limit=limit)
    raise ValueError(f"Unsupported entity_type: {entity_type}")


async def run_once(entity_type: str, dry_run: bool = False) -> int:
    db = Database()
    await db.init()
    limit = int(os.getenv("TRIGGER_BATCH_LIMIT", str(DEFAULT_LIMIT)))
    session_key = os.getenv("OPENCLAW_CALLBACK_SESSION_KEY", DEFAULT_SESSION_KEY).strip() or DEFAULT_SESSION_KEY

    items = await fetch_candidates(db, entity_type, limit)
    if not items:
        logging.info("No %s to callback", entity_type)
        return 0

    fingerprint = build_fingerprint(entity_type, items)
    payload = build_payload(entity_type, items, fingerprint)
    message = build_message(payload)
    idempotency_key = f"parserUserBot:{entity_type}:{fingerprint}"

    batch = await db.create_or_get_callback_batch(entity_type, fingerprint, payload)
    if not batch:
        logging.warning("Failed to create/get callback batch")
        return -1

    if batch.get("status") == "sent":
        logging.info("Batch already sent earlier: %s", fingerprint)
        await db.mark_entities_as_notified(entity_type, [int(item["id"]) for item in items])
        return 0

    if dry_run:
        logging.info("DRY RUN outbox => %s", message)
        return len(items)

    try:
        outbox = await db.enqueue_callback_outbox(
            entity_type=entity_type,
            batch_id=int(batch["id"]),
            session_key=session_key,
            message=message,
            idempotency_key=idempotency_key,
            max_attempts=10,
        )
        await db.mark_entities_as_notified(entity_type, [int(item["id"]) for item in items])
        logging.info("Enqueued callback outbox id=%s for %s items", outbox.get("id") if outbox else "?", len(items))
        return len(items)
    except Exception as exc:
        logging.exception("Callback outbox enqueue failed")
        await db.mark_callback_batch_failed(int(batch["id"]), str(exc))
        return -1


async def run_forever(entity_type: str, dry_run: bool = False) -> None:
    poll_interval = float(os.getenv("TRIGGER_POLL_INTERVAL_SEC", "30"))
    while True:
        await run_once(entity_type=entity_type, dry_run=dry_run)
        await asyncio.sleep(poll_interval)


def main() -> int:
    setup_logging()
    args = parse_args()
    try:
        if args.once:
            result = asyncio.run(run_once(entity_type=args.entity_type, dry_run=args.dry_run))
            return 1 if result < 0 else 0
        asyncio.run(run_forever(entity_type=args.entity_type, dry_run=args.dry_run))
        return 0
    except KeyboardInterrupt:
        logging.info("Stopped by user")
        return 0
    except Exception:
        logging.exception("Trigger worker crashed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
