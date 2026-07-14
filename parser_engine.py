from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

import httpx

AI_SYSTEM_PROMPT = (
    "You are a strict binary classifier for Telegram posts. Your task is to decide whether the provided Telegram post "
    "matches the user's selection criteria. Rules: Return exactly one lowercase word: true or false. Do not explain "
    "your answer. Do not add punctuation, markdown, JSON, quotes, or extra text. Return true only if the post clearly "
    "matches the user's criteria. Return false if the post is irrelevant, ambiguous, promotional spam, unrelated, or "
    "there is not enough information. Negative constraints in the user's criteria always override positive signals. "
    "Classify only the provided post. Do not infer missing facts."
)

MatchMode = Literal["keyword", "ai"]
KeywordPolicy = Literal["any", "all"]


def normalize_chat_ids(values: Any) -> set[str]:
    """Normalize chat ids from list/tuple/set/comma-separated string to strings."""
    if values is None:
        return set()
    if isinstance(values, str):
        raw = [v.strip() for v in values.split(",")]
    elif isinstance(values, (list, tuple, set)):
        raw = [str(v).strip() for v in values]
    else:
        raw = [str(values).strip()]
    return {v for v in raw if v}


def should_skip_chat(chat_id: int | str, config: dict[str, Any], *, is_private: bool = False) -> bool:
    """Return True if chat must be ignored before classification.

    Supported config keys:
    - source_chat_ids: allow-list. If non-empty, all other chats are skipped.
    - config.excluded_chat_ids / config.exclude_chat_ids: deny-list.
    - config.ignore_private: skip private/direct messages when true.
    """
    chat = str(chat_id)
    cfg = config.get("config") or {}
    if is_private and bool(cfg.get("ignore_private", False)):
        return True
    excluded = normalize_chat_ids(cfg.get("excluded_chat_ids") or cfg.get("exclude_chat_ids"))
    if chat in excluded:
        return True
    source_ids = normalize_chat_ids(config.get("source_chat_ids") or cfg.get("source_chat_ids"))
    return bool(source_ids) and chat not in source_ids


@dataclass(slots=True)
class MatchResult:
    matched: bool
    reason: str = ""
    error: str | None = None


def _contains(text: str, pattern: str, *, case_sensitive: bool = False, regex: bool = False) -> bool:
    if not pattern:
        return False
    flags = 0 if case_sensitive else re.IGNORECASE
    if regex:
        return re.search(pattern, text, flags=flags) is not None
    haystack = text if case_sensitive else text.lower()
    needle = pattern if case_sensitive else pattern.lower()
    return needle in haystack


def match_keywords(
    text: str,
    keywords: list[str] | tuple[str, ...] | None,
    negative_keywords: list[str] | tuple[str, ...] | None = None,
    *,
    policy: KeywordPolicy = "any",
    case_sensitive: bool = False,
    regex: bool = False,
) -> MatchResult:
    """Evaluate deterministic keyword rules.

    Negative keywords always override positive matches. Empty positive keyword list never matches.
    """
    text = text or ""
    positives = [k for k in (keywords or []) if k]
    negatives = [k for k in (negative_keywords or []) if k]

    for keyword in negatives:
        try:
            if _contains(text, keyword, case_sensitive=case_sensitive, regex=regex):
                return MatchResult(False, reason=f"negative keyword matched: {keyword}")
        except re.error as exc:
            return MatchResult(False, reason="invalid negative regex", error=str(exc))

    if not positives:
        return MatchResult(False, reason="no keywords configured")

    try:
        checks = [_contains(text, k, case_sensitive=case_sensitive, regex=regex) for k in positives]
    except re.error as exc:
        return MatchResult(False, reason="invalid keyword regex", error=str(exc))

    matched = all(checks) if policy == "all" else any(checks)
    return MatchResult(matched, reason=f"keyword policy {policy} {'matched' if matched else 'did not match'}")


def parse_strict_bool(value: str | None) -> bool | None:
    """Return True/False only for lowercase true/false with optional surrounding whitespace."""
    if value is None:
        return None
    normalized = value.strip()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    return None


class AIClassifier:
    def __init__(self, *, base_url: str, api_key: str, model: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    async def classify(self, text: str, user_prompt: str, *, system_prompt: str = AI_SYSTEM_PROMPT) -> MatchResult:
        if not self.base_url or not self.api_key or not self.model:
            return MatchResult(False, reason="AI is not configured", error="missing base_url/api_key/model")
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Criteria:\n{user_prompt}\n\nTelegram post:\n{text}"},
            ],
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]
        except Exception as exc:  # network/provider/schema errors are non-matches
            return MatchResult(False, reason="AI request failed", error=str(exc))

        parsed = parse_strict_bool(content)
        if parsed is None:
            return MatchResult(False, reason="AI returned non-strict boolean", error=f"invalid response: {content!r}")
        return MatchResult(parsed, reason="AI returned true" if parsed else "AI returned false")


async def match_parser_config(text: str, config: dict[str, Any], ai_classifier: AIClassifier | None = None) -> MatchResult:
    mode = config.get("mode", "keyword")
    cfg = config.get("config") or {}
    if mode == "keyword":
        return match_keywords(
            text,
            cfg.get("keywords") or [],
            cfg.get("negative_keywords") or [],
            policy=cfg.get("policy", "any"),
            case_sensitive=bool(cfg.get("case_sensitive", False)),
            regex=bool(cfg.get("regex", False)),
        )
    if mode == "ai":
        if ai_classifier is None:
            return MatchResult(False, reason="AI classifier unavailable", error="ai_classifier is None")
        return await ai_classifier.classify(
            text,
            cfg.get("user_prompt") or cfg.get("prompt") or "",
            system_prompt=cfg.get("system_prompt") or AI_SYSTEM_PROMPT,
        )
    return MatchResult(False, reason=f"unsupported mode: {mode}", error="unsupported mode")
