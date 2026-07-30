from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from browser_worker.captcha import CaptchaDetector, CaptchaEvidence
from browser_worker.evaluator import PageSnapshot
from browser_worker.form_mapper import FormMapping, MappedField

from .base import BrowserRuntime, FormField, SubmitObservation


class FormInteractionError(RuntimeError):
    pass


@dataclass(frozen=True)
class FormReadback:
    values: Mapping[str, object]
    mismatched_refs: tuple[str, ...]

    @property
    def matches(self) -> bool:
        return not self.mismatched_refs


class GenericFormAdapter:
    """Site-neutral adapter operating on metadata and opaque field refs only."""

    def __init__(
        self,
        runtime: BrowserRuntime,
        *,
        captcha_detector: CaptchaDetector | None = None,
    ):
        self.runtime = runtime
        self.captcha_detector = captcha_detector or CaptchaDetector()

    @property
    def session_key(self) -> str:
        return self.runtime.session_key

    async def navigate(self, url: str) -> None:
        await self.runtime.navigate(url)

    async def current_url(self) -> str:
        return await self.runtime.current_url()

    async def snapshot(self, *, max_text_chars: int) -> PageSnapshot:
        return await self.runtime.snapshot(max_text_chars=max_text_chars)

    async def fields(self) -> tuple[FormField, ...]:
        fields = await self.runtime.extract_form_fields()
        refs: set[str] = set()
        result: list[FormField] = []
        for form_field in fields:
            if not form_field.ref or form_field.ref in refs:
                raise FormInteractionError("form field refs must be non-empty and unique")
            refs.add(form_field.ref)
            result.append(form_field)
        return tuple(result)

    async def captcha(self) -> CaptchaEvidence:
        metadata = await self.runtime.captcha_metadata(
            selectors=self.captcha_detector.selectors
        )
        return self.captcha_detector.detect(metadata)

    @staticmethod
    def _expected_value(mapped: MappedField) -> object:
        if mapped.field.input_type.casefold() == "checkbox":
            if not isinstance(mapped.value, bool):
                raise FormInteractionError(
                    f"checkbox {mapped.field.ref} requires an explicit boolean"
                )
            return mapped.value
        return str(mapped.value)

    async def fill(
        self,
        mapping: FormMapping,
        *,
        resume_path: Path,
    ) -> dict[str, object]:
        if not mapping.can_fill:
            raise FormInteractionError("form contains unknown required fields")
        expected: dict[str, object] = {}
        for mapped in mapping.mapped_fields:
            field_type = mapped.field.input_type.casefold()
            value = self._expected_value(mapped)
            if field_type == "checkbox":
                await self.runtime.set_checked(mapped.field.ref, bool(value))
            elif field_type in {"select", "select-one"}:
                await self.runtime.select_option(mapped.field.ref, str(value))
            elif field_type in {
                "text",
                "email",
                "tel",
                "url",
                "number",
                "search",
                "textarea",
                "date",
            }:
                await self.runtime.fill_text(mapped.field.ref, str(value))
            else:
                raise FormInteractionError(
                    f"unsupported mapped field type: {field_type}"
                )
            expected[mapped.field.ref] = value

        if mapping.resume_fields:
            await self.runtime.upload_file(mapping.resume_fields[0].ref, resume_path)
        return expected

    async def verify_readback(self, expected: Mapping[str, object]) -> FormReadback:
        values = await self.runtime.read_form_values()
        mismatched: list[str] = []
        for ref, expected_value in expected.items():
            actual = values.get(ref)
            if isinstance(expected_value, bool):
                if actual is not expected_value:
                    mismatched.append(ref)
            elif str(actual) != str(expected_value):
                mismatched.append(ref)
        return FormReadback(values=values, mismatched_refs=tuple(mismatched))

    async def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        await self.runtime.screenshot(path)

    async def submit_once(self) -> SubmitObservation:
        return await self.runtime.submit_once()

    async def close(self, *, preserve_session: bool) -> None:
        await self.runtime.close(preserve_session=preserve_session)
