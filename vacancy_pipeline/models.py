from __future__ import annotations

import datetime
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class SourceType(StrEnum):
    TELEGRAM = "telegram"
    HH = "hh"
    HABR = "habr"
    GETMATCH = "getmatch"
    CAREERSPACE = "careerspace"
    DIRECT = "direct"


class ProcessingStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class RemoteType(StrEnum):
    REMOTE_ONLY = "remote_only"
    HYBRID = "hybrid"
    OFFICE = "office"
    UNKNOWN = "unknown"


class AIVerdict(StrEnum):
    QUALIFIED = "qualified"
    GARBAGE = "garbage"
    NEEDS_REVIEW = "needs_review"


class VacancyState(StrEnum):
    PENDING = "pending"
    QUALIFIED = "qualified"
    NEEDS_REVIEW = "needs_review"
    GARBAGE = "garbage"
    APPROVED_FOR_BROWSER = "approved_for_browser"
    SUBMITTED = "submitted"
    REJECTED = "rejected"


class ApplicationState(StrEnum):
    PENDING = "pending"
    APPROVED_FOR_BROWSER = "approved_for_browser"
    PROCESSING_BROWSER = "processing_browser"
    CAPTCHA_REQUIRED = "captcha_required"
    READY_FOR_SUBMIT_APPROVAL = "ready_for_submit_approval"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    NEEDS_HUMAN_ANSWER = "needs_human_answer"
    NEEDS_MANUAL_VERIFICATION = "needs_manual_verification"
    REJECTED = "rejected"


class CheckpointState(StrEnum):
    WAITING_HUMAN = "waiting_human"
    RESOLVED = "resolved"
    ABORTED = "aborted"


@dataclass
class VacancyIntake:
    id: int
    source_type: SourceType
    source_chat_id: int | None
    source_message_id: int | None
    source_channel: str | None
    message_url: str | None
    raw_text: str
    raw_payload: dict[str, Any]
    idempotency_key: str
    content_hash: str
    processing_status: ProcessingStatus
    retry_count: int
    last_error: str | None
    locked_by: str | None
    locked_until: datetime.datetime | None
    next_attempt_at: datetime.datetime
    created_at: datetime.datetime
    updated_at: datetime.datetime


@dataclass
class JobVacancy:
    id: int
    intake_id: int
    company: str | None
    title: str | None
    canonical_url: str | None
    official_apply_url: str | None
    description_text: str
    remote_type: RemoteType
    salary_min_rub: int | None
    salary_max_rub: int | None
    salary_hidden: bool
    stack_tags: list[str]
    seniority: str | None
    ai_verdict: AIVerdict
    ai_confidence: float
    ai_score: int
    ai_reason: str
    rule_reason: str
    state: VacancyState
    is_submitted: bool
    submitted_at: datetime.datetime | None
    external_confirmation: str | None
    created_at: datetime.datetime
    updated_at: datetime.datetime


@dataclass
class Application:
    id: int
    vacancy_id: int
    profile_key: str
    application_key: str
    state: ApplicationState
    resume_path: str
    resume_sha256: str
    form_payload: dict[str, Any]
    form_payload_hash: str
    cover_letter: str | None
    approved_payload_hash: str | None
    approved_resume_sha256: str | None
    approved_at: datetime.datetime | None
    is_submitted: bool
    submitted_at: datetime.datetime | None
    external_confirmation: str | None
    last_error: str | None
    retry_count: int
    locked_by: str | None
    locked_until: datetime.datetime | None
    next_attempt_at: datetime.datetime | None
    created_at: datetime.datetime
    updated_at: datetime.datetime


@dataclass
class ApplicationEvent:
    id: int
    application_id: int
    old_state: ApplicationState | None
    new_state: ApplicationState
    actor: str
    event_type: str
    metadata: dict[str, Any]
    created_at: datetime.datetime


@dataclass
class CaptchaCheckpoint:
    id: int
    application_id: int
    state: CheckpointState
    page_url: str
    screenshot_path: str | None
    persistent_session_key: str
    instructions: str
    created_at: datetime.datetime
    resolved_at: datetime.datetime | None


@dataclass
class SubmissionProof:
    id: int
    application_id: int
    before_screenshot_path: str | None
    after_screenshot_path: str | None
    confirmation_text: str | None
    confirmation_url: str | None
    provider_application_id: str | None
    uploaded_resume_sha256: str
    submitted_payload_hash: str
    proof_hash: str
    created_at: datetime.datetime


@dataclass
class LLMCall:
    id: int
    purpose: str
    vacancy_id: int | None
    application_id: int | None
    model: str
    prompt_hash: str
    input_hash: str
    status: str
    cache_hit: bool
    input_tokens: int | None
    output_tokens: int | None
    response: dict[str, Any]
    created_at: datetime.datetime
