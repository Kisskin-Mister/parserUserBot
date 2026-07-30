from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from browser_worker.adapters.base import CaptchaPageMetadata


DEFAULT_CAPTCHA_SELECTORS = (
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    ".g-recaptcha",
    ".h-captcha",
    "#cf-challenge-running",
    "[data-sitekey]",
)


@dataclass(frozen=True)
class CaptchaEvidence:
    detected: bool
    markers: tuple[str, ...]


class CaptchaDetector:
    IFRAME_MARKERS = ("recaptcha", "hcaptcha", "challenges.cloudflare.com")
    CLASS_MARKERS = (
        "g-recaptcha",
        "h-captcha",
        "cf-challenge",
        "cloudflare-challenge",
    )
    TEXT_MARKERS = (
        "prove you are human",
        "verify you are human",
        "checking your browser",
        "проверка что вы человек",
        "подтвердите что вы человек",
        "проверка безопасности",
        "капча",
        "captcha",
    )

    def __init__(self, selectors: Sequence[str] = DEFAULT_CAPTCHA_SELECTORS):
        self.selectors = tuple(selectors)

    def detect(self, metadata: CaptchaPageMetadata) -> CaptchaEvidence:
        markers: list[str] = []
        for source in metadata.iframe_sources:
            source_lower = source.casefold()
            if any(marker in source_lower for marker in self.IFRAME_MARKERS):
                markers.append(f"iframe:{source[:200]}")
        for class_name in metadata.class_names:
            class_lower = class_name.casefold()
            if any(marker in class_lower for marker in self.CLASS_MARKERS):
                markers.append(f"class:{class_name[:100]}")
        text = " ".join(metadata.visible_text.casefold().split())
        for marker in self.TEXT_MARKERS:
            if marker in text:
                markers.append(f"text:{marker}")
        markers.extend(f"selector:{selector}" for selector in metadata.matched_selectors)
        return CaptchaEvidence(bool(markers), tuple(dict.fromkeys(markers)))


def captcha_notification_payload(
    *,
    application_id: int,
    company: str | None,
    title: str | None,
    url: str,
    screenshot_path: str,
    instructions: str,
) -> dict[str, object]:
    """Stable payload suitable for an n8n/Telegram notification hook."""

    return {
        "event_type": "application_captcha_required",
        "application_id": application_id,
        "company": company,
        "title": title,
        "url": url,
        "screenshot_path": screenshot_path,
        "instructions": instructions,
    }
