from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from vacancy_pipeline.ai_judge import StructuredAIJudge, VacancyJudgeResult
from vacancy_pipeline.rules import (
    canonical_json_hash,
    enforce_nazar_policy,
    normalized_content_hash,
    obvious_garbage,
)
from vacancy_pipeline.schemas import TelegramVacancyEvent


class PipelineRepository(Protocol):
    async def create_intake(self, **values: Any) -> tuple[dict[str, Any], bool]: ...

    async def claim_intakes(self, worker_id: str, limit: int, lease_seconds: int) -> list[dict[str, Any]]: ...

    async def create_vacancy(self, intake_id: int, **values: Any) -> dict[str, Any]: ...

    async def mark_intake_completed(self, intake_id: int) -> None: ...

    async def mark_intake_failed(self, intake_id: int, error: str) -> None: ...


class VacancyJudge(Protocol):
    async def evaluate(
        self,
        text: str,
        *,
        cache: Any,
        vacancy_id: int | None = None,
        application_id: int | None = None,
    ) -> VacancyJudgeResult: ...


@dataclass(frozen=True, slots=True)
class IngestResult:
    intake: dict[str, Any]
    created: bool

    @property
    def status(self) -> str:
        return "created" if self.created else "duplicate"


@dataclass(frozen=True, slots=True)
class BatchResult:
    claimed: int
    processed: int
    failed: int


class ResumePolicyError(ValueError):
    pass


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def configured_resume_allowlist() -> tuple[Path, ...]:
    raw = os.getenv("RESUME_ALLOWLIST", "")
    legacy = os.getenv("RESUME_PATH", "")
    values = [item.strip() for item in raw.split(os.pathsep) if item.strip()]
    if legacy:
        values.append(legacy)
    return tuple(Path(item).expanduser().resolve() for item in values)


def validate_resume(path_value: str, expected_sha256: str | None, allowlist: tuple[Path, ...]) -> tuple[str, str]:
    if not allowlist:
        raise ResumePolicyError("RESUME_ALLOWLIST is not configured")
    path = Path(path_value).expanduser().resolve()
    allowed = {candidate.resolve() for candidate in allowlist}
    if path not in allowed:
        raise ResumePolicyError("resume path is not allowlisted")
    if not path.is_file():
        raise ResumePolicyError("allowlisted resume file does not exist")
    actual_hash = sha256_file(path)
    if expected_sha256 and actual_hash != expected_sha256.casefold():
        raise ResumePolicyError("resume SHA-256 does not match")
    return str(path), actual_hash


class VacancyPipelineService:
    def __init__(
        self,
        repository: PipelineRepository,
        judge: VacancyJudge,
        *,
        ai_max_calls_per_batch: int | None = None,
        lease_seconds: int = 300,
        resume_allowlist: tuple[Path, ...] | None = None,
    ) -> None:
        self.repository = repository
        self.judge = judge
        configured_cap = int(os.getenv("AI_MAX_CALLS_PER_BATCH", "20"))
        self.ai_max_calls_per_batch = max(1, ai_max_calls_per_batch or configured_cap)
        self.lease_seconds = lease_seconds
        self.resume_allowlist = configured_resume_allowlist() if resume_allowlist is None else resume_allowlist

    async def ingest(self, event: TelegramVacancyEvent) -> IngestResult:
        source = event.source
        payload = event.model_dump(mode="json")
        intake, created = await self.repository.create_intake(
            source_type="telegram",
            source_chat_id=int(source.chat_id),
            source_message_id=int(source.message_id),
            source_channel=source.channel,
            message_url=source.message_url,
            raw_text=event.post_text,
            raw_payload=payload,
            idempotency_key=event.idempotency_key,
            content_hash=normalized_content_hash(event.post_text),
        )
        return IngestResult(intake=intake, created=created)

    @staticmethod
    def _vacancy_from_result(intake: dict[str, Any], result: VacancyJudgeResult, rule_reason: str = "") -> dict[str, Any]:
        enforced = enforce_nazar_policy(result)
        state = enforced.verdict
        return {
            "company": enforced.company or None,
            "title": enforced.title or None,
            "canonical_url": intake.get("message_url"),
            "official_apply_url": enforced.official_url,
            "description_text": intake["raw_text"],
            "remote_type": enforced.remote_type,
            "salary_min_rub": enforced.salary_min_rub,
            "salary_max_rub": enforced.salary_max_rub,
            "salary_hidden": enforced.salary_hidden,
            "stack_tags": enforced.stack_tags,
            "seniority": enforced.seniority,
            "ai_verdict": enforced.verdict,
            "ai_confidence": enforced.confidence,
            "ai_score": enforced.score,
            "ai_reason": enforced.reason,
            "rule_reason": rule_reason,
            "state": state,
        }

    @staticmethod
    def _rule_garbage_values(intake: dict[str, Any], reason: str, confidence: float) -> dict[str, Any]:
        return {
            "company": None,
            "title": None,
            "canonical_url": intake.get("message_url"),
            "official_apply_url": None,
            "description_text": intake["raw_text"],
            "remote_type": "unknown",
            "salary_min_rub": None,
            "salary_max_rub": None,
            "salary_hidden": True,
            "stack_tags": [],
            "seniority": "unknown",
            "ai_verdict": "garbage",
            "ai_confidence": confidence,
            "ai_score": 0,
            "ai_reason": "Rejected by deterministic high-confidence rule",
            "rule_reason": reason,
            "state": "garbage",
        }

    @staticmethod
    def _ai_failure_values(intake: dict[str, Any], exc: Exception) -> dict[str, Any]:
        return {
            "company": None,
            "title": None,
            "canonical_url": intake.get("message_url"),
            "official_apply_url": None,
            "description_text": intake["raw_text"],
            "remote_type": "unknown",
            "salary_min_rub": None,
            "salary_max_rub": None,
            "salary_hidden": True,
            "stack_tags": [],
            "seniority": "unknown",
            "ai_verdict": "needs_review",
            "ai_confidence": 0.0,
            "ai_score": 0,
            "ai_reason": f"AI evaluation failed ({type(exc).__name__}); manual review required",
            "rule_reason": "",
            "state": "needs_review",
        }

    async def process_batch(self, worker_id: str, *, limit: int | None = None) -> BatchResult:
        claim_limit = min(limit or self.ai_max_calls_per_batch, self.ai_max_calls_per_batch)
        intakes = await self.repository.claim_intakes(worker_id, claim_limit, self.lease_seconds)
        processed = 0
        failed = 0
        for intake in intakes:
            try:
                rule = obvious_garbage(intake["raw_text"])
                if rule.is_garbage:
                    values = self._rule_garbage_values(intake, rule.reason, rule.confidence)
                else:
                    try:
                        judged = await self.judge.evaluate(intake["raw_text"], cache=self.repository)
                        values = self._vacancy_from_result(intake, judged)
                    except Exception as exc:
                        # Provider/network/strict-schema failures are never classified as garbage.
                        values = self._ai_failure_values(intake, exc)
                await self.repository.create_vacancy(intake["id"], **values)
                await self.repository.mark_intake_completed(intake["id"])
                processed += 1
            except Exception as exc:
                await self.repository.mark_intake_failed(intake["id"], f"{type(exc).__name__}: {exc}"[:2_000])
                failed += 1
        return BatchResult(claimed=len(intakes), processed=processed, failed=failed)

    async def set_human_decision(
        self,
        vacancy_id: int,
        *,
        verdict: str,
        reason: str,
        approved_for_browser: bool,
        actor: str = "api_admin",
    ) -> dict[str, Any] | None:
        state = "approved_for_browser" if approved_for_browser else verdict
        return await self.repository.update_vacancy(
            vacancy_id,
            ai_verdict=verdict,
            ai_reason=reason,
            state=state,
            human_actor=actor,
        )

    async def prepare_application(
        self,
        vacancy_id: int,
        *,
        profile_key: str,
        resume_path: str,
        expected_resume_sha256: str | None,
        form_payload: dict[str, Any],
        cover_letter: str | None,
    ) -> dict[str, Any]:
        vacancy = await self.repository.get_vacancy(vacancy_id)
        if not vacancy:
            raise LookupError("vacancy not found")
        validated_path, resume_hash = validate_resume(resume_path, expected_resume_sha256, self.resume_allowlist)
        application = await self.repository.prepare_application(
            vacancy_id=vacancy_id,
            profile_key=profile_key,
            resume_path=validated_path,
            resume_sha256=resume_hash,
            form_payload=form_payload,
            cover_letter=cover_letter,
        )
        if vacancy.get("state") == "approved_for_browser" and application.get("state") == "pending":
            transitioned = await self.repository.transition_application(
                application["id"],
                new_state="approved_for_browser",
                actor="pipeline_service",
                event_type="browser_processing_approved",
                metadata={"vacancy_id": vacancy_id},
                expected_states=["pending"],
            )
            if transitioned:
                application = transitioned
        return application

    async def update_application_payload(self, application_id: int, payload: dict[str, Any]) -> dict[str, Any] | None:
        return await self.repository.update_application_payload(
            application_id,
            form_payload=payload,
            form_payload_hash=canonical_json_hash(payload),
        )

    async def approve_exact_application(
        self,
        application_id: int,
        *,
        payload_hash: str,
        resume_sha256: str,
        actor: str = "api_admin",
    ) -> dict[str, Any] | None:
        return await self.repository.approve_application_exact(
            application_id,
            payload_hash=payload_hash.casefold(),
            resume_sha256=resume_sha256.casefold(),
            actor=actor,
        )


def build_structured_judge_from_env() -> StructuredAIJudge:
    return StructuredAIJudge(
        base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        api_key=os.getenv("OPENAI_API_KEY", ""),
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        timeout=float(os.getenv("AI_REQUEST_TIMEOUT_SECONDS", "45")),
        max_input_chars=int(os.getenv("AI_MAX_INPUT_CHARS", "50000")),
    )
