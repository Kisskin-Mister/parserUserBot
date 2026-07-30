from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError


@dataclass(frozen=True)
class PageSnapshot:
    url: str
    title: str
    main_text: str
    body_text: str
    business_signals: tuple[str, ...] = ()

    def bounded_text(self, max_chars: int = 24_000) -> str:
        text = self.main_text.strip() or self.body_text.strip()
        return text[:max_chars]


class PageEvaluation(BaseModel):
    """Strict second-pass verdict based on the rendered vacancy page."""

    model_config = ConfigDict(extra="forbid", strict=True)

    active: bool
    target_role: Literal[
        "java_backend", "go_backend", "backend_other", "not_target"
    ]
    remote_type: Literal["remote_only", "hybrid", "office", "unknown"]
    salary_min_rub: int | None = Field(default=None, ge=0)
    salary_max_rub: int | None = Field(default=None, ge=0)
    verdict: Literal["qualified", "garbage", "needs_review"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=1000)
    risks: list[str] = Field(default_factory=list, max_length=30)


class PageEvaluationError(RuntimeError):
    pass


class PageEvaluationClient(Protocol):
    async def evaluate(self, snapshot: PageSnapshot) -> PageEvaluation: ...


PAGE_EVALUATOR_SYSTEM_PROMPT = """You are a strict vacancy page evaluator.
Use only the rendered page text supplied by the caller. Never infer missing facts.
The target is a currently active, fully remote, middle+/senior Java or Go backend
role. Hybrid, office, closed, non-target, spam, or fake jobs are not qualified.
Return exactly one JSON object matching the supplied schema, with no markdown.
"""


class OpenAIPageEvaluator:
    """OpenAI-compatible strict JSON evaluator.

    Only title, URL, rendered text, and pre-extracted business signals are sent;
    page HTML, cookies, form values, and candidate PII are never included.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 30.0,
        max_text_chars: int = 24_000,
        client: httpx.AsyncClient | None = None,
    ):
        if not base_url or not api_key or not model:
            raise ValueError("base_url, api_key, and model are required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_text_chars = max_text_chars
        self._client = client

    async def evaluate(self, snapshot: PageSnapshot) -> PageEvaluation:
        body = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": PAGE_EVALUATOR_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "url": snapshot.url,
                            "title": snapshot.title[:500],
                            "rendered_text": snapshot.bounded_text(
                                self.max_text_chars
                            ),
                            "business_signals": list(snapshot.business_signals),
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "vacancy_page_evaluation",
                    "strict": True,
                    "schema": PageEvaluation.model_json_schema(),
                },
            },
        }
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.timeout_seconds)
        try:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=body,
            )
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("message content is not a string")
            return PageEvaluation.model_validate_json(content)
        except (httpx.HTTPError, KeyError, TypeError, ValueError, ValidationError) as exc:
            raise PageEvaluationError(f"page evaluator failed: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()
