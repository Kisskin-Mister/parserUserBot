from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vacancy_pipeline.ai_judge import VacancyJudgeResult


@dataclass(frozen=True, slots=True)
class RuleDecision:
    is_garbage: bool
    reason: str = ""
    confidence: float = 0.0


_OBVIOUS_GARBAGE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(курс|обучени[ея]|вебинар|bootcamp)\b", re.IGNORECASE), "education/course promotion"),
    (
        re.compile(r"\b(дайджест|подборк[аи]|топ[- ]?ваканси|вакансии за неделю|weekly digest)\b", re.IGNORECASE),
        "vacancy digest or collection",
    ),
    (re.compile(r"\b(резюме|ищу работу|open to work)\b", re.IGNORECASE), "candidate/resume post"),
    (re.compile(r"\b(новост[ьи]|релиз|обзор рынка|митап|конференци[яи])\b", re.IGNORECASE), "news or event post"),
)

_JOB_CONTEXT = re.compile(
    r"\b(ваканси[яию]|ищем|hiring|job|position|developer|разработчик|engineer|backend|golang|java)\b",
    re.IGNORECASE,
)


def normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text or "")
    return " ".join(normalized.casefold().split())


def normalized_content_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def canonical_json_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def obvious_garbage(text: str) -> RuleDecision:
    normalized = normalize_text(text)
    if len(normalized) < 12:
        return RuleDecision(True, "empty or too short to be a vacancy", 1.0)
    for pattern, reason in _OBVIOUS_GARBAGE_PATTERNS:
        if pattern.search(normalized):
            return RuleDecision(True, reason, 0.99)
    if not _JOB_CONTEXT.search(normalized) and len(normalized) < 240:
        return RuleDecision(True, "no job/vacancy context", 0.98)
    return RuleDecision(False)


def enforce_nazar_policy(result: VacancyJudgeResult) -> VacancyJudgeResult:
    """Apply non-negotiable constraints after the model's structured extraction."""
    risks = list(dict.fromkeys(result.risks))
    verdict = result.verdict
    reason = result.reason

    def reject(policy_reason: str) -> None:
        nonlocal verdict, reason
        verdict = "garbage"
        reason = policy_reason

    if not result.is_single_vacancy:
        reject("Post is not a single vacancy")
    elif not result.is_real_job:
        reject("Post is not a real job")
    elif result.target_role not in {"java_backend", "go_backend"}:
        reject("Java or Go is not the primary backend technology")
    elif result.remote_type in {"hybrid", "office"}:
        reject("Role is not fully remote")
    elif result.salary_max_rub is not None and result.salary_max_rub < 300_000:
        reject("Maximum salary is below 300000 RUB")

    if verdict != "garbage":
        if result.remote_type == "unknown":
            verdict = "needs_review"
            reason = "Remote-only status is not confirmed"
            risks.append("remote_unknown")
        if result.seniority in {"junior", "intern"}:
            reject("Seniority is below middle+")
        elif result.seniority == "middle":
            verdict = "needs_review"
            reason = "Middle+ seniority is not confirmed"
            risks.append("seniority_borderline")
        elif result.seniority == "unknown":
            verdict = "needs_review"
            reason = "Middle+ seniority is not confirmed"
            risks.append("seniority_unknown")
        if result.salary_min_rub is not None and result.salary_min_rub < 300_000:
            verdict = "needs_review"
            reason = "Salary range starts below 300000 RUB"
            risks.append("salary_floor_below_minimum")
        if result.salary_hidden:
            risks.append("salary_hidden")
        if "иннотех" in result.company.casefold() or "innotech" in result.company.casefold():
            verdict = "needs_review"
            reason = "Innotech is excluded from automatic processing"
            risks.append("company_no_auto_apply")

    return result.model_copy(update={"verdict": verdict, "reason": reason, "risks": list(dict.fromkeys(risks))})
