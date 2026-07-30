from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Sequence

from browser_worker.evaluator import PageSnapshot


@dataclass(frozen=True)
class FormField:
    """Non-sensitive form metadata exposed to the mapper.

    ``ref`` is an opaque page-local identifier.  The generic adapter deliberately
    does not expose full HTML, scripts, or arbitrary DOM attributes to an AI
    mapper.
    """

    ref: str
    label: str
    input_type: str
    name: str = ""
    placeholder: str = ""
    autocomplete: str = ""
    required: bool = False
    options: tuple[str, ...] = field(default_factory=tuple)

    def metadata(self) -> dict[str, object]:
        return {
            "ref": self.ref,
            "label": self.label,
            "input_type": self.input_type,
            "name": self.name,
            "placeholder": self.placeholder,
            "autocomplete": self.autocomplete,
            "required": self.required,
            "options": list(self.options),
        }


@dataclass(frozen=True)
class CaptchaPageMetadata:
    iframe_sources: tuple[str, ...] = field(default_factory=tuple)
    class_names: tuple[str, ...] = field(default_factory=tuple)
    visible_text: str = ""
    matched_selectors: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SubmitObservation:
    """Result observed after the one permitted click.

    A changed URL alone is deliberately not a business confirmation.
    ``business_confirmed`` must be based on a provider success marker or explicit
    success text found by the adapter.
    """

    clicked: bool
    business_confirmed: bool
    confirmation_text: str | None = None
    confirmation_url: str | None = None
    provider_application_id: str | None = None


class BrowserRuntime(Protocol):
    """Minimal injectable browser surface used by the generic adapter."""

    session_key: str

    async def navigate(self, url: str) -> None: ...

    async def current_url(self) -> str: ...

    async def snapshot(self, *, max_text_chars: int) -> PageSnapshot: ...

    async def extract_form_fields(self) -> Sequence[FormField]: ...

    async def captcha_metadata(
        self, *, selectors: Sequence[str]
    ) -> CaptchaPageMetadata: ...

    async def fill_text(self, field_ref: str, value: str) -> None: ...

    async def select_option(self, field_ref: str, value: str) -> None: ...

    async def set_checked(self, field_ref: str, checked: bool) -> None: ...

    async def upload_file(self, field_ref: str, path: Path) -> None: ...

    async def read_form_values(self) -> dict[str, object]: ...

    async def screenshot(self, path: Path) -> None: ...

    async def submit_once(self) -> SubmitObservation: ...

    async def close(self, *, preserve_session: bool) -> None: ...


class BrowserRuntimeFactory(Protocol):
    async def create(self, *, session_key: str) -> BrowserRuntime: ...
