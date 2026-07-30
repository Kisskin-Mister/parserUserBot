from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

import api
import main


class FakeTelegramDb:
    def __init__(self):
        self.commands: dict[int, dict] = {}
        self.next_id = 1
        self.pool = SimpleNamespace(close=AsyncMock())

    async def init(self):
        return None

    async def enqueue_command(self, command_type, target, data, dedupe_key):
        for row in self.commands.values():
            if row.get("dedupe_key") == dedupe_key:
                return row | {"deduplicated": True}
        row = {
            "id": self.next_id,
            "command_type": command_type,
            "target": target,
            "data": data,
            "dedupe_key": dedupe_key,
            "status": "pending",
            "telegram_message_id": None,
            "result": None,
            "error": None,
            "created_at": None,
            "updated_at": None,
            "processed_at": None,
            "deduplicated": False,
        }
        self.commands[self.next_id] = row
        self.next_id += 1
        return row

    async def get_command(self, command_id):
        return self.commands.get(command_id)


def build_client(fake_db: FakeTelegramDb, tmp_path: Path) -> TestClient:
    api.ADMIN_TOKEN = "secret-test-token"
    api.db = fake_db
    api.TELEGRAM_ALLOWED_FILE_ROOTS = str(tmp_path)
    return TestClient(api.app)


def test_post_telegram_message_requires_admin(tmp_path):
    client = build_client(FakeTelegramDb(), tmp_path)

    response = client.post("/telegram/messages", json={"target": "@hr", "text": "hello", "idempotency_key": "k1"})

    assert response.status_code == 401


def test_post_telegram_message_enqueues_idempotently(tmp_path):
    fake_db = FakeTelegramDb()
    client = build_client(fake_db, tmp_path)
    headers = {"Authorization": "Bearer secret-test-token"}
    body = {"target": "@hr", "text": "hello", "idempotency_key": "same-key"}

    first = client.post("/telegram/messages", headers=headers, json=body)
    second = client.post("/telegram/messages", headers=headers, json=body)

    assert first.status_code == 201
    assert first.json()["command_id"] == 1
    assert first.json()["status"] == "pending"
    assert first.json()["deduplicated"] is False
    assert second.status_code == 200
    assert second.json()["command_id"] == 1
    assert second.json()["deduplicated"] is True
    assert len(fake_db.commands) == 1


def test_post_telegram_document_validates_allowed_path_and_payload(tmp_path):
    fake_db = FakeTelegramDb()
    client = build_client(fake_db, tmp_path)
    headers = {"Authorization": "Bearer secret-test-token"}
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")

    response = client.post(
        "/telegram/documents",
        headers=headers,
        json={"target": "123456", "file_path": str(resume), "caption": "resume", "idempotency_key": "doc-key"},
    )

    assert response.status_code == 201
    command = fake_db.commands[1]
    assert command["command_type"] == "send_document"
    assert command["target"] == "123456"
    assert '"file_path"' in command["data"]
    assert '"caption"' in command["data"]


def test_post_telegram_document_rejects_outside_allowed_roots(tmp_path):
    fake_db = FakeTelegramDb()
    client = build_client(fake_db, tmp_path)
    headers = {"Authorization": "Bearer secret-test-token"}
    outside = Path("/tmp/parser-userbot-outside-test.pdf")
    outside.write_bytes(b"x")
    try:
        response = client.post(
            "/telegram/documents",
            headers=headers,
            json={"target": "@hr", "file_path": str(outside), "idempotency_key": "outside"},
        )
        assert response.status_code == 400
    finally:
        outside.unlink(missing_ok=True)


def test_get_telegram_command_returns_result_fields(tmp_path):
    fake_db = FakeTelegramDb()
    fake_db.commands[3] = {
        "id": 3,
        "command_type": "send_message",
        "target": "@hr",
        "data": "hello",
        "dedupe_key": "k3",
        "status": "done",
        "telegram_message_id": 777,
        "result": {"telegram_message_id": 777},
        "error": None,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:01",
        "processed_at": "2026-01-01T00:00:02",
    }
    client = build_client(fake_db, tmp_path)

    response = client.get("/telegram/commands/3", headers={"Authorization": "Bearer secret-test-token"})

    assert response.status_code == 200
    assert response.json()["telegram_message_id"] == 777
    assert response.json()["status"] == "done"


class FakeSent:
    id = 4242


class FakeClient:
    def __init__(self):
        self.message_calls = []
        self.document_calls = []

    async def send_message(self, target, text):
        self.message_calls.append((target, text))
        return FakeSent()

    async def send_document(self, target, document, caption=None):
        self.document_calls.append((target, document, caption))
        return FakeSent()



def test_process_telegram_command_saves_message_id_and_caption():
    fake_db = AsyncMock()
    command = {
        "id": 10,
        "command_type": "send_document",
        "target": "@hr",
        "data": '{"file_path":"/tmp/resume.pdf","caption":"CV"}',
    }
    client = FakeClient()

    asyncio.run(main.process_telegram_command(client, fake_db, command))

    assert client.document_calls == [("hr", "/tmp/resume.pdf", "CV")]
    fake_db.mark_command_done.assert_awaited_once_with(
        10,
        status="done",
        telegram_message_id=4242,
        error=None,
        result={"telegram_message_id": 4242},
    )



def test_process_telegram_command_saves_exception_text():
    class FailingClient(FakeClient):
        async def send_message(self, target, text):
            raise RuntimeError("telegram says no")

    fake_db = AsyncMock()
    command = {"id": 11, "command_type": "send_message", "target": "@hr", "data": "hello"}

    asyncio.run(main.process_telegram_command(FailingClient(), fake_db, command))

    fake_db.mark_command_done.assert_awaited_once_with(
        11,
        status="failed",
        telegram_message_id=None,
        error="telegram says no",
        result={"error": "telegram says no"},
    )


def test_database_command_migration_adds_result_error_timestamps_columns():
    source = Path("database.py").read_text()

    for expected in [
        "telegram_message_id",
        "result JSONB",
        "error TEXT",
        "created_at TIMESTAMP",
        "updated_at TIMESTAMP",
        "processed_at TIMESTAMP",
        "claim_pending_commands",
    ]:
        assert expected in source
