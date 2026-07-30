from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vacancy_pipeline.rules import canonical_json_hash, normalized_content_hash


JUDGE_SYSTEM_PROMPT = """You extract and evaluate exactly one vacancy for Nazar. Return one JSON object only, with exactly the requested fields and no markdown. Never infer missing facts. Target: middle+/senior Java or Go backend, fully remote, at least 300000 RUB. Hidden salary is a risk rather than an automatic rejection. Hybrid, office, relocation-only, non-Java/Go-primary roles, spam, digests and fake jobs are garbage. Innotech must never be auto-qualified; return needs_review. The verdict must be qualified, garbage, or needs_review and reason must be short and concrete."""


class VacancyJudgeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    is_single_vacancy: bool
    is_real_job: bool
    target_role: Literal["java_backend", "go_backend", "backend_other", "not_target"]
    company: str = Field(max_length=500)
    title: str = Field(max_length=500)
    remote_type: Literal["remote_only", "hybrid", "office", "unknown"]
    salary_min_rub: int | None = Field(default=None, ge=0)
    salary_max_rub: int | None = Field(default=None, ge=0)
    salary_hidden: bool
    seniority: Literal["intern", "junior", "middle", "middle_plus", "senior", "lead", "unknown"]
    stack_tags: list[str] = Field(max_length=100)
    official_url: str | None = Field(default=None, max_length=4_000)
    verdict: Literal["qualified", "garbage", "needs_review"]
    score: int = Field(ge=0, le=100)
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=2_000)
    risks: list[str] = Field(default_factory=list, max_length=100)


class LLMCache(Protocol):
    async def get_cached_llm_call(self, *, purpose: str, model: str, input_hash: str) -> dict[str, Any] | None: ...

    async def record_llm_call(self, **values: Any) -> Any: ...


class AIJudgeError(RuntimeError):
    pass


Transport = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None


class StructuredAIJudge:
    purpose = "telegram_vacancy_judge"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 45.0,
        max_input_chars: int = 50_000,
        transport: Transport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_input_chars = max_input_chars
        self.transport = transport
        self.prompt_hash = hashlib.sha256(JUDGE_SYSTEM_PROMPT.encode("utf-8")).hexdigest()

    def _payload(self, text: str) -> dict[str, Any]:
        return {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": f"Vacancy post:\n{text[: self.max_input_chars]}"},
            ],
        }

    async def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.transport is not None:
            return await self.transport(payload)
        if not self.base_url or not self.api_key or not self.model:
            raise AIJudgeError("structured AI judge is not configured")
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                response.raise_for_status()
                return response.json()
        except Exception as exc:
            raise AIJudgeError(f"provider request failed: {type(exc).__name__}") from exc

    @staticmethod
    def _parse(data: dict[str, Any]) -> tuple[VacancyJudgeResult, ProviderUsage]:
        try:
            content = data["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("message content is not text")
            raw = json.loads(content)
            if not isinstance(raw, dict):
                raise TypeError("response JSON is not an object")
            result = VacancyJudgeResult.model_validate(raw)
            usage_raw = data.get("usage") or {}
            usage = ProviderUsage(
                input_tokens=usage_raw.get("prompt_tokens") or usage_raw.get("input_tokens"),
                output_tokens=usage_raw.get("completion_tokens") or usage_raw.get("output_tokens"),
            )
            return result, usage
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError, ValidationError) as exc:
            raise AIJudgeError("provider returned invalid structured JSON") from exc

    async def evaluate(
        self,
        text: str,
        *,
        cache: LLMCache,
        vacancy_id: int | None = None,
        application_id: int | None = None,
    ) -> VacancyJudgeResult:
        input_hash = normalized_content_hash(text)
        cached = await cache.get_cached_llm_call(purpose=self.purpose, model=self.model, input_hash=input_hash)
        if cached:
            try:
                return VacancyJudgeResult.model_validate(cached["response"])
            except (KeyError, ValidationError, TypeError, ValueError):
                # A corrupt/legacy cache row must not prevent a fresh provider call.
                pass

        data = await self._request(self._payload(text))
        result, usage = self._parse(data)
        response_json = result.model_dump(mode="json")
        await cache.record_llm_call(
            purpose=self.purpose,
            vacancy_id=vacancy_id,
            application_id=application_id,
            model=self.model,
            prompt_hash=self.prompt_hash,
            input_hash=input_hash,
            cache_hit=False,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            response=response_json,
        )
        return result


def structured_input_hash(value: dict[str, Any]) -> str:
    """Hash structured page/form inputs without depending on provider serialization."""
    return canonical_json_hash(value)
