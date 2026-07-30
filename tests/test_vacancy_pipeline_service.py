from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import pytest

from vacancy_pipeline.ai_judge import AIJudgeError, StructuredAIJudge, VacancyJudgeResult
from vacancy_pipeline.rules import canonical_json_hash, enforce_nazar_policy, normalized_content_hash
from vacancy_pipeline.schemas import TelegramVacancyEvent
from vacancy_pipeline.service import VacancyPipelineService


QUALIFIED = {
    "is_single_vacancy": True,
    "is_real_job": True,
    "target_role": "java_backend",
    "company": "Example",
    "title": "Senior Java Developer",
    "remote_type": "remote_only",
    "salary_min_rub": 350000,
    "salary_max_rub": 450000,
    "salary_hidden": False,
    "seniority": "senior",
    "stack_tags": ["Java", "Spring"],
    "official_url": "https://jobs.example.test/java",
    "verdict": "qualified",
    "score": 91,
    "confidence": 0.94,
    "reason": "Senior remote Java role with sufficient salary",
    "risks": [],
}


class MemoryRepository:
    def __init__(self) -> None:
        self.intakes: list[dict] = []
        self.vacancies: list[dict] = []
        self.cache: dict[tuple[str, str, str], dict] = {}

    async def create_intake(self, **values):
        for row in self.intakes:
            if row["idempotency_key"] == values["idempotency_key"]:
                return deepcopy(row), False
        row = {"id": len(self.intakes) + 1, "processing_status": "pending", **values}
        self.intakes.append(row)
        return deepcopy(row), True

    async def claim_intakes(self, worker_id: str, limit: int, lease_seconds: int):
        del worker_id, lease_seconds
        claimed = []
        for row in self.intakes:
            if row["processing_status"] == "pending" and len(claimed) < limit:
                row["processing_status"] = "processing"
                claimed.append(deepcopy(row))
        return claimed

    async def create_vacancy(self, intake_id: int, **values):
        row = {"id": len(self.vacancies) + 1, "intake_id": intake_id, **values}
        self.vacancies.append(row)
        return deepcopy(row)

    async def mark_intake_completed(self, intake_id: int):
        self.intakes[intake_id - 1]["processing_status"] = "completed"

    async def mark_intake_failed(self, intake_id: int, error: str):
        self.intakes[intake_id - 1].update(processing_status="failed", last_error=error)

    async def get_cached_llm_call(self, *, purpose: str, model: str, input_hash: str):
        return deepcopy(self.cache.get((purpose, model, input_hash)))

    async def record_llm_call(self, **values):
        response = deepcopy(values["response"])
        self.cache[(values["purpose"], values["model"], values["input_hash"])] = {
            **values,
            "response": response,
        }
        return values


class FakeJudge:
    def __init__(self, result: VacancyJudgeResult | Exception) -> None:
        self.result = result
        self.calls = 0

    async def evaluate(self, text: str, *, cache, vacancy_id=None, application_id=None):
        del text, cache, vacancy_id, application_id
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def event(text: str, message_id: str = "456") -> TelegramVacancyEvent:
    return TelegramVacancyEvent.model_validate(
        {
            "event_type": "telegram_vacancy_post",
            "idempotency_key": f"telegram:-100123:{message_id}",
            "source": {
                "chat_id": "-100123",
                "message_id": message_id,
                "channel": "jobs",
                "message_url": f"https://t.me/jobs/{message_id}",
            },
            "post_text": text,
            "attachments": [],
            "raw": {"update_id": 1},
        }
    )


def test_idempotent_telegram_ingestion_returns_original_intake():
    async def scenario():
        repo = MemoryRepository()
        service = VacancyPipelineService(repo, FakeJudge(VacancyJudgeResult.model_validate(QUALIFIED)))
        first = await service.ingest(event("Senior Java backend, remote"))
        second = await service.ingest(event("changed duplicate payload"))
        return repo, first, second

    repo, first, second = asyncio.run(scenario())

    assert first.created is True
    assert second.created is False
    assert first.intake["id"] == second.intake["id"]
    assert len(repo.intakes) == 1
    assert repo.intakes[0]["content_hash"] == normalized_content_hash("Senior Java backend, remote")


def test_obvious_garbage_skips_llm_but_is_auditable():
    async def scenario():
        repo = MemoryRepository()
        judge = FakeJudge(VacancyJudgeResult.model_validate(QUALIFIED))
        service = VacancyPipelineService(repo, judge)
        await service.ingest(event("Курс Java с гарантией трудоустройства. Купите обучение!"))
        result = await service.process_batch("worker-1")
        return repo, judge, result

    repo, judge, result = asyncio.run(scenario())

    assert result.processed == 1
    assert judge.calls == 0
    assert repo.vacancies[0]["ai_verdict"] == "garbage"
    assert repo.vacancies[0]["rule_reason"]


def test_plausible_job_always_invokes_structured_judge():
    async def scenario():
        repo = MemoryRepository()
        judge = FakeJudge(VacancyJudgeResult.model_validate(QUALIFIED))
        service = VacancyPipelineService(repo, judge)
        await service.ingest(event("Ищем Senior Java backend developer, fully remote, 400000 RUB"))
        await service.process_batch("worker-1")
        return repo, judge

    repo, judge = asyncio.run(scenario())

    assert judge.calls == 1
    assert repo.vacancies[0]["state"] == "qualified"
    assert repo.vacancies[0]["ai_reason"] == QUALIFIED["reason"]


def test_provider_or_malformed_failure_becomes_needs_review_not_garbage():
    async def scenario():
        repo = MemoryRepository()
        judge = FakeJudge(AIJudgeError("provider timeout"))
        service = VacancyPipelineService(repo, judge)
        await service.ingest(event("Senior Golang backend vacancy, удалённо"))
        await service.process_batch("worker-1")
        return repo

    repo = asyncio.run(scenario())

    assert repo.vacancies[0]["ai_verdict"] == "needs_review"
    assert repo.vacancies[0]["state"] == "needs_review"
    assert "AI evaluation failed" in repo.vacancies[0]["ai_reason"]


def test_structured_ai_cache_avoids_second_provider_call():
    async def scenario():
        repo = MemoryRepository()
        calls = 0

        async def transport(payload):
            nonlocal calls
            calls += 1
            assert payload["temperature"] == 0
            return {
                "choices": [{"message": {"content": json.dumps(QUALIFIED)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            }

        judge = StructuredAIJudge(
            base_url="https://provider.example/v1",
            api_key="test-placeholder",
            model="judge-model",
            transport=transport,
        )
        first = await judge.evaluate("same vacancy", cache=repo)
        second = await judge.evaluate("same vacancy", cache=repo)
        return first, second, calls

    first, second, calls = asyncio.run(scenario())

    assert first == second
    assert calls == 1


def test_canonical_payload_hash_is_stable_and_changes_with_answers():
    first = canonical_json_hash({"email": "candidate@example.test", "name": "N"})
    reordered = canonical_json_hash({"name": "N", "email": "candidate@example.test"})
    changed = canonical_json_hash({"name": "N", "email": "other@example.test"})

    assert first == reordered
    assert first != changed


def test_malformed_structured_response_raises_retryable_judge_error():
    async def scenario():
        async def transport(_payload):
            return {"choices": [{"message": {"content": "```json\n{}\n```"}}]}

        judge = StructuredAIJudge(
            base_url="https://provider.example/v1",
            api_key="placeholder",
            model="judge-model",
            transport=transport,
        )
        return await judge.evaluate("Senior Java vacancy", cache=MemoryRepository())

    with pytest.raises(AIJudgeError, match="invalid structured JSON"):
        asyncio.run(scenario())


def test_policy_guardrails_override_office_and_innotech_auto_qualification():
    office = VacancyJudgeResult.model_validate({**QUALIFIED, "remote_type": "office"})
    innotech = VacancyJudgeResult.model_validate({**QUALIFIED, "company": "Иннотех"})

    assert enforce_nazar_policy(office).verdict == "garbage"
    guarded = enforce_nazar_policy(innotech)
    assert guarded.verdict == "needs_review"
    assert "company_no_auto_apply" in guarded.risks
