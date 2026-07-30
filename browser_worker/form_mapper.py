from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Protocol, Sequence, TypeAlias

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from browser_worker.adapters.base import FormField

ProfileValue: TypeAlias = str | bool | int


ALLOWED_PROFILE_KEYS = frozenset(
    {
        "full_name",
        "first_name",
        "last_name",
        "email",
        "phone",
        "location",
        "linkedin_url",
        "github_url",
        "telegram",
        "website",
        "current_company",
        "current_title",
        "years_experience",
        "salary_expectation",
        "cover_letter",
        "work_authorization",
        "consent",
    }
)


@dataclass(frozen=True)
class CandidateProfile:
    """Explicit allowlisted answers; the mapper cannot invent new values."""

    values: Mapping[str, ProfileValue]
    allowed_resume_paths: tuple[Path, ...]

    def __post_init__(self) -> None:
        unknown = set(self.values) - ALLOWED_PROFILE_KEYS
        if unknown:
            raise ValueError(f"unsupported candidate profile keys: {sorted(unknown)}")
        if not self.allowed_resume_paths:
            raise ValueError("at least one allowlisted resume path is required")
        for key, value in self.values.items():
            if isinstance(value, str) and not value.strip():
                raise ValueError(f"profile value {key!r} must not be blank")

    def value(self, key: str) -> ProfileValue | None:
        return self.values.get(key)

    def is_resume_allowed(self, resume_path: Path) -> bool:
        resolved = resume_path.resolve(strict=True)
        return any(
            resolved == allowed.resolve(strict=True)
            for allowed in self.allowed_resume_paths
        )


@dataclass(frozen=True)
class MappedField:
    field: FormField
    profile_key: str
    value: ProfileValue


@dataclass(frozen=True)
class FormMapping:
    mapped_fields: tuple[MappedField, ...]
    resume_fields: tuple[FormField, ...]
    unknown_required: tuple[FormField, ...]
    skipped_optional: tuple[FormField, ...] = field(default_factory=tuple)

    @property
    def can_fill(self) -> bool:
        return not self.unknown_required and len(self.resume_fields) <= 1


class FieldMappingDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    field_ref: str = Field(min_length=1, max_length=200)
    profile_key: str | None


class FieldMappingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mappings: list[FieldMappingDecision] = Field(max_length=100)


class UnknownFieldMappingClient(Protocol):
    async def map_fields(
        self, fields: Sequence[FormField], available_profile_keys: Sequence[str]
    ) -> Sequence[FieldMappingDecision]: ...


class FieldMappingError(RuntimeError):
    pass


UNKNOWN_FIELD_SYSTEM_PROMPT = """Map form metadata to candidate profile keys.
You receive metadata only, never HTML or candidate values. Select only a key in
available_profile_keys. If a field cannot be mapped with high confidence, return
null. Never invent an answer or transform one field into free-form content. Return
strict JSON matching the schema.
"""


class OpenAIUnknownFieldMapper:
    """Optional mapper for labels not covered by deterministic aliases."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 30.0,
    ):
        if not base_url or not api_key or not model:
            raise ValueError("base_url, api_key, and model are required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._client = client
        self.timeout_seconds = timeout_seconds

    async def map_fields(
        self, fields: Sequence[FormField], available_profile_keys: Sequence[str]
    ) -> Sequence[FieldMappingDecision]:
        safe_fields = [field.metadata() for field in fields]
        body = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": UNKNOWN_FIELD_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "fields": safe_fields,
                            "available_profile_keys": list(available_profile_keys),
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "form_field_mapping",
                    "strict": True,
                    "schema": FieldMappingResponse.model_json_schema(),
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
            return FieldMappingResponse.model_validate_json(content).mappings
        except (httpx.HTTPError, KeyError, TypeError, ValueError, ValidationError) as exc:
            raise FieldMappingError(f"unknown field mapper failed: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()


def _normalise(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"[^a-zа-я0-9]+", " ", value)
    return " ".join(value.split())


def _field_haystack(field: FormField) -> str:
    return _normalise(
        " ".join(
            (
                field.label,
                field.name,
                field.placeholder,
                field.autocomplete,
            )
        )
    )


class DeterministicFormMapper:
    """Allowlist-first mapper with an optional metadata-only AI fallback."""

    _PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("email", (r"\be mail\b", r"\bemail\b", r"электронн\w* почт", r"почта")),
        ("phone", (r"\bphone\b", r"\btel\b", r"телефон", r"мобильн")),
        ("first_name", (r"\bfirst name\b", r"\bgiven name\b", r"имя кандидата")),
        ("last_name", (r"\blast name\b", r"\bfamily name\b", r"фамили")),
        ("full_name", (r"\bfull name\b", r"\byour name\b", r"\bfio\b", r"\bфио\b")),
        ("linkedin_url", (r"linkedin",)),
        ("github_url", (r"github", r"git hub")),
        ("telegram", (r"telegram", r"телеграм")),
        ("website", (r"portfolio", r"website", r"сайт", r"портфолио")),
        ("location", (r"location", r"city", r"город", r"локаци")),
        ("current_company", (r"current company", r"текущ\w* компани")),
        ("current_title", (r"current title", r"current role", r"текущ\w* должност")),
        ("years_experience", (r"years of experience", r"лет опыта", r"опыт.*лет")),
        ("salary_expectation", (r"salary", r"compensation", r"зарплат", r"доход")),
        ("cover_letter", (r"cover letter", r"message", r"сопровод", r"мотивац")),
        ("work_authorization", (r"work authori[sz]ation", r"разрешен\w* на работу")),
        ("consent", (r"consent", r"privacy", r"personal data", r"соглас", r"персональн\w* данн")),
    )

    _AUTOCOMPLETE_KEYS = {
        "email": "email",
        "tel": "phone",
        "given-name": "first_name",
        "family-name": "last_name",
        "name": "full_name",
        "street-address": "location",
        "address-level2": "location",
        "url": "website",
    }

    def __init__(self, ai_mapper: UnknownFieldMappingClient | None = None):
        self.ai_mapper = ai_mapper

    def _deterministic_key(self, field: FormField) -> str | None:
        field_type = field.input_type.casefold()
        if field_type == "email":
            return "email"
        if field_type == "tel":
            return "phone"
        autocomplete = field.autocomplete.strip().casefold().split()
        for token in autocomplete:
            if token in self._AUTOCOMPLETE_KEYS:
                return self._AUTOCOMPLETE_KEYS[token]
        haystack = _field_haystack(field)
        for key, patterns in self._PATTERNS:
            if any(re.search(pattern, haystack) for pattern in patterns):
                return key
        if haystack in {"name", "имя"}:
            return "full_name"
        return None

    async def map_fields(
        self, fields: Sequence[FormField], profile: CandidateProfile
    ) -> FormMapping:
        mapped: list[MappedField] = []
        unknown: list[FormField] = []
        skipped: list[FormField] = []
        resume_fields: list[FormField] = []
        ignored_types = {"hidden", "submit", "button", "reset", "image"}

        for form_field in fields:
            field_type = form_field.input_type.casefold()
            if field_type in ignored_types:
                continue
            if field_type == "file":
                if re.search(r"\b(resume|cv)\b|резюме", _field_haystack(form_field)):
                    resume_fields.append(form_field)
                elif form_field.required:
                    unknown.append(form_field)
                else:
                    skipped.append(form_field)
                continue
            if field_type in {"password", "radio"}:
                (unknown if form_field.required else skipped).append(form_field)
                continue
            key = self._deterministic_key(form_field)
            value = profile.value(key) if key else None
            if key is not None and value is not None:
                mapped.append(MappedField(form_field, key, value))
            else:
                unknown.append(form_field)

        if unknown and self.ai_mapper:
            decisions = await self.ai_mapper.map_fields(
                unknown, sorted(profile.values.keys())
            )
            by_ref = {item.field_ref: item.profile_key for item in decisions}
            still_unknown: list[FormField] = []
            for form_field in unknown:
                key = by_ref.get(form_field.ref)
                value = profile.value(key) if key in ALLOWED_PROFILE_KEYS else None
                if key and key in profile.values and value is not None:
                    mapped.append(MappedField(form_field, key, value))
                else:
                    still_unknown.append(form_field)
            unknown = still_unknown

        required = [form_field for form_field in unknown if form_field.required]
        skipped.extend(form_field for form_field in unknown if not form_field.required)
        if len(resume_fields) > 1:
            required.extend(resume_fields)
        return FormMapping(
            mapped_fields=tuple(mapped),
            resume_fields=tuple(resume_fields),
            unknown_required=tuple(required),
            skipped_optional=tuple(skipped),
        )
