from __future__ import annotations

from copy import deepcopy

from fastapi import FastAPI
from fastapi.testclient import TestClient

from vacancy_pipeline.api import create_pipeline_router


class APIRepository:
    def __init__(self) -> None:
        self.intakes: dict[str, dict] = {}
        self.list_args: dict | None = None

    async def create_intake(self, **values):
        existing = self.intakes.get(values["idempotency_key"])
        if existing:
            return deepcopy(existing), False
        row = {"id": len(self.intakes) + 1, "processing_status": "pending", **values}
        self.intakes[values["idempotency_key"]] = row
        return deepcopy(row), True

    async def list_vacancies(self, **values):
        self.list_args = values
        return []


class UnusedJudge:
    async def evaluate(self, *args, **kwargs):  # pragma: no cover - intake does not judge inline
        raise AssertionError("judge must be called only by the vacancy worker")


async def allow_admin() -> None:
    return None


def build_client(repository: APIRepository) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_pipeline_router(
            repository_factory=lambda: repository,
            judge_factory=UnusedJudge,
            admin_dependency=allow_admin,
        )
    )
    return TestClient(app)


def payload() -> dict:
    return {
        "event_type": "telegram_vacancy_post",
        "idempotency_key": "telegram:-100123:456",
        "source": {
            "chat_id": "-100123",
            "message_id": "456",
            "channel": "jobs",
            "message_url": "https://t.me/jobs/456",
        },
        "post_text": "Senior Java backend, remote only",
        "attachments": [],
        "raw": {},
    }


def test_ingest_endpoint_distinguishes_created_and_duplicate():
    repository = APIRepository()
    client = build_client(repository)

    created = client.post("/vacancy-events", json=payload())
    duplicate = client.post("/vacancy-events", json=payload())

    assert created.status_code == 201
    assert created.json() == {"status": "created", "intake_id": 1, "processing_status": "pending"}
    assert duplicate.status_code == 200
    assert duplicate.json() == {"status": "duplicate", "intake_id": 1, "processing_status": "pending"}
    assert len(repository.intakes) == 1


def test_ingest_rejects_noncanonical_telegram_idempotency_key():
    client = build_client(APIRepository())
    body = payload()
    body["idempotency_key"] = "random-key"

    response = client.post("/vacancy-events", json=body)

    assert response.status_code == 422


def test_working_list_excludes_garbage_unless_explicitly_requested():
    repository = APIRepository()
    client = build_client(repository)

    assert client.get("/job-vacancies").status_code == 200
    assert repository.list_args is not None
    assert repository.list_args["include_garbage"] is False

    assert client.get("/job-vacancies?verdict=garbage").status_code == 200
    assert repository.list_args["include_garbage"] is True
