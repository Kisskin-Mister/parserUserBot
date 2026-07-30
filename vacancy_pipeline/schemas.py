from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vacancy_pipeline.models import AIVerdict, CheckpointState


class TelegramSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: str = Field(pattern=r"^-?\d+$")
    message_id: str = Field(pattern=r"^\d+$")
    channel: str | None = None
    message_url: str | None = None


class TelegramVacancyEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: Literal["telegram_vacancy_post"]
    idempotency_key: str = Field(min_length=1, max_length=300)
    source: TelegramSource
    post_text: str = Field(min_length=1, max_length=200_000)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_stable_key(self) -> TelegramVacancyEvent:
        expected = f"telegram:{self.source.chat_id}:{self.source.message_id}"
        if self.idempotency_key != expected:
            raise ValueError(f"idempotency_key must equal {expected}")
        return self


class HumanVacancyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: AIVerdict
    reason: str = Field(min_length=1, max_length=2_000)
    approved_for_browser: bool = False


class ApplicationPrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_key: str = Field(default="default", pattern=r"^[A-Za-z0-9_.-]{1,80}$")
    resume_path: str = Field(min_length=1, max_length=2_000)
    resume_sha256: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")
    form_payload: dict[str, Any] = Field(default_factory=dict)
    cover_letter: str | None = Field(default=None, max_length=10_000)


class ExactApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload_hash: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    resume_sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")


class ApplicationPayloadUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    form_payload: dict[str, Any]


class CaptchaResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal[CheckpointState.RESOLVED, CheckpointState.ABORTED]
    note: str | None = Field(default=None, max_length=2_000)
