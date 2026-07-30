from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.encoders import jsonable_encoder

from vacancy_pipeline.schemas import (
    ApplicationPayloadUpdate,
    ApplicationPrepareRequest,
    CaptchaResolutionRequest,
    ExactApprovalRequest,
    HumanVacancyDecision,
    TelegramVacancyEvent,
)
from vacancy_pipeline.service import ResumePolicyError, VacancyPipelineService


def _safe_application(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    resume_path = result.pop("resume_path", None)
    payload = result.pop("form_payload", None) or {}
    result.pop("cover_letter", None)
    result["resume_file"] = Path(resume_path).name if resume_path else None
    result["form_payload_keys"] = sorted(payload)
    return result


def create_pipeline_router(
    *,
    repository_factory: Callable[[], Any],
    judge_factory: Callable[[], Any],
    admin_dependency: Callable[..., Any],
) -> APIRouter:
    router = APIRouter(tags=["vacancy-pipeline"])

    def repository() -> Any:
        return repository_factory()

    def service() -> VacancyPipelineService:
        return VacancyPipelineService(repository(), judge_factory())

    admin = [Depends(admin_dependency)]

    @router.post("/vacancy-events", dependencies=admin)
    async def ingest_event(event: TelegramVacancyEvent, response: Response) -> dict[str, Any]:
        result = await service().ingest(event)
        response.status_code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
        return {
            "status": result.status,
            "intake_id": result.intake["id"],
            "processing_status": result.intake["processing_status"],
        }

    @router.get("/job-vacancies")
    async def list_vacancies(
        verdict: str | None = Query(default=None, pattern="^(qualified|garbage|needs_review)$"),
        state: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> Any:
        rows = await repository().list_vacancies(
            verdict=verdict,
            state=state,
            include_garbage=verdict == "garbage" or state == "garbage",
            limit=limit,
            offset=offset,
        )
        return jsonable_encoder(rows)

    @router.get("/job-vacancies/{vacancy_id}")
    async def get_vacancy(vacancy_id: int) -> Any:
        row = await repository().get_vacancy(vacancy_id)
        if not row:
            raise HTTPException(status_code=404, detail="vacancy not found")
        return jsonable_encoder(row)

    @router.patch("/job-vacancies/{vacancy_id}/decision", dependencies=admin)
    async def decide_vacancy(vacancy_id: int, decision: HumanVacancyDecision) -> Any:
        row = await service().set_human_decision(
            vacancy_id,
            verdict=decision.verdict.value,
            reason=decision.reason,
            approved_for_browser=decision.approved_for_browser,
        )
        if not row:
            raise HTTPException(status_code=404, detail="vacancy not found")
        return jsonable_encoder(row)

    @router.post("/job-vacancies/{vacancy_id}/applications", dependencies=admin, status_code=201)
    async def prepare_application(vacancy_id: int, request: ApplicationPrepareRequest) -> Any:
        try:
            row = await service().prepare_application(
                vacancy_id,
                profile_key=request.profile_key,
                resume_path=request.resume_path,
                expected_resume_sha256=request.resume_sha256,
                form_payload=request.form_payload,
                cover_letter=request.cover_letter,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ResumePolicyError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return jsonable_encoder(_safe_application(row))

    @router.get("/applications/{application_id}", dependencies=admin)
    async def get_application(application_id: int) -> Any:
        row = await repository().get_application(application_id)
        if not row:
            raise HTTPException(status_code=404, detail="application not found")
        return jsonable_encoder(_safe_application(row))

    @router.put("/applications/{application_id}/payload", dependencies=admin)
    async def update_application_payload(application_id: int, request: ApplicationPayloadUpdate) -> Any:
        row = await service().update_application_payload(application_id, request.form_payload)
        if not row:
            raise HTTPException(status_code=404, detail="application not found")
        return jsonable_encoder(_safe_application(row))

    @router.post("/applications/{application_id}/approve-exact", dependencies=admin)
    async def approve_exact(application_id: int, request: ExactApprovalRequest) -> Any:
        try:
            row = await service().approve_exact_application(
                application_id,
                payload_hash=request.payload_hash,
                resume_sha256=request.resume_sha256,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not row:
            raise HTTPException(status_code=404, detail="application not found")
        return jsonable_encoder(_safe_application(row))

    @router.get("/captcha-checkpoints", dependencies=admin)
    async def list_captcha_checkpoints(
        checkpoint_state: str | None = Query(default="waiting_human", alias="state"),
        application_id: int | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> Any:
        rows = await repository().list_captcha_checkpoints(
            state=checkpoint_state,
            application_id=application_id,
            limit=limit,
        )
        return jsonable_encoder(rows)

    @router.post("/captcha-checkpoints/{checkpoint_id}/resolve", dependencies=admin)
    async def resolve_captcha(checkpoint_id: int, request: CaptchaResolutionRequest) -> Any:
        row = await repository().resolve_captcha_checkpoint(
            checkpoint_id,
            state=request.state.value if hasattr(request.state, "value") else request.state,
            actor="api_admin",
            note=request.note,
        )
        if not row:
            raise HTTPException(status_code=409, detail="checkpoint not found or is no longer waiting")
        return jsonable_encoder(row)

    @router.get("/applications/{application_id}/events", dependencies=admin)
    async def application_events(
        application_id: int,
        limit: int = Query(default=200, ge=1, le=1_000),
    ) -> Any:
        return jsonable_encoder(await repository().list_application_events(application_id, limit=limit))

    @router.get("/applications/{application_id}/proof", dependencies=admin)
    async def application_proof(application_id: int) -> Any:
        proof = await repository().get_submission_proof(application_id)
        if not proof:
            raise HTTPException(status_code=404, detail="submission proof not found")
        return jsonable_encoder(proof)

    @router.get("/llm-calls", dependencies=admin)
    async def llm_calls(limit: int = Query(default=100, ge=1, le=500)) -> Any:
        return jsonable_encoder(await repository().list_llm_calls(limit=limit))

    return router
