from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from vacancy_pipeline.models import ApplicationState

from browser_worker.adapters.base import BrowserRuntimeFactory
from browser_worker.adapters.generic import FormInteractionError, GenericFormAdapter
from browser_worker.captcha import captcha_notification_payload
from browser_worker.evaluator import (
    PageEvaluation,
    PageEvaluationClient,
    PageEvaluationError,
)
from browser_worker.form_mapper import (
    CandidateProfile,
    DeterministicFormMapper,
    FieldMappingError,
    FormMapping,
)
from browser_worker.security import URLValidationError, ValidatedURL


class URLValidator(Protocol):
    async def validate(
        self, url: str, *, enforce_domain_allowlist: bool = True
    ) -> ValidatedURL: ...


class BrowserRepository(Protocol):
    """PostgreSQL repository contract used by the browser worker.

    Implementations must make claim, state/event transitions, checkpoint creation,
    and proof+submitted mutation atomic.  ``claim_browser_application`` may claim
    only ``approved_for_browser`` and transitions it to ``processing_browser``.
    """

    async def claim_browser_application(
        self, worker_id: str, lease_seconds: int = 300
    ) -> Mapping[str, Any] | None: ...

    async def get_application(
        self, application_id: int
    ) -> Mapping[str, Any] | None: ...

    async def get_vacancy(
        self, vacancy_id: int
    ) -> Mapping[str, Any] | None: ...

    async def transition_application(
        self,
        application_id: int,
        new_state: str,
        *,
        actor: str,
        event_type: str,
        metadata: Mapping[str, Any] | None = None,
        last_error: str | None = None,
        expected_states: list[str] | None = None,
    ) -> Mapping[str, Any]: ...

    async def reject_vacancy_for_browser(
        self,
        vacancy_id: int,
        application_id: int,
        *,
        reason: str,
        actor: str = "browser_worker",
    ) -> Mapping[str, Any]: ...

    async def update_prepared_application(
        self,
        application_id: int,
        *,
        form_payload: Mapping[str, Any],
        form_payload_hash: str,
        resume_sha256: str | None = None,
        actor: str = "browser_worker",
        metadata: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]: ...

    async def create_captcha_checkpoint(
        self,
        application_id: int,
        *,
        page_url: str,
        screenshot_path: str,
        persistent_session_key: str,
        instructions: str,
        actor: str = "browser_worker",
    ) -> Mapping[str, Any]: ...

    async def record_submission_proof_and_mark_submitted(
        self,
        application_id: int,
        *,
        before_screenshot_path: str | None = None,
        after_screenshot_path: str | None = None,
        confirmation_text: str | None = None,
        confirmation_url: str | None = None,
        provider_application_id: str | None = None,
        uploaded_resume_sha256: str,
        submitted_payload_hash: str,
        proof_hash: str | None = None,
        actor: str = "browser_worker",
    ) -> Mapping[str, Any]: ...

    async def mark_manual_verification(
        self,
        application_id: int,
        *,
        error: str,
        actor: str = "browser_worker",
        metadata: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]: ...

    async def release_application(
        self,
        application_id: int,
        worker_id: str,
        *,
        next_attempt_at: Any | None = None,
        error: str | None = None,
        state: str = "approved_for_browser",
    ) -> Mapping[str, Any]: ...


class CaptchaNotifier(Protocol):
    async def notify(self, payload: Mapping[str, object]) -> None: ...


@dataclass(frozen=True)
class BrowserWorkerConfig:
    worker_id: str = "browser-worker"
    lease_seconds: int = 300
    final_submit_enabled: bool = False
    artifacts_dir: Path = Path("runtime/browser_worker")
    max_page_text_chars: int = 24_000
    keep_context_open_on_captcha: bool = True
    captcha_instructions: str = (
        "Open the same persistent browser session, solve CAPTCHA manually, then "
        "mark the checkpoint resolved. The worker will verify CAPTCHA again."
    )


@dataclass(frozen=True)
class BrowserWorkerOutcome:
    application_id: int
    status: str
    detail: str
    keep_session_open: bool = False


def canonical_json_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _record_value(record: Any, key: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(key, default)
    return getattr(record, key, default)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class BrowserWorker:
    def __init__(
        self,
        *,
        repository: BrowserRepository,
        runtime_factory: BrowserRuntimeFactory,
        evaluator: PageEvaluationClient,
        profile: CandidateProfile,
        url_guard: URLValidator,
        mapper: DeterministicFormMapper | None = None,
        notifier: CaptchaNotifier | None = None,
        config: BrowserWorkerConfig | None = None,
    ):
        self.repository = repository
        self.runtime_factory = runtime_factory
        self.evaluator = evaluator
        self.profile = profile
        self.url_guard = url_guard
        self.mapper = mapper or DeterministicFormMapper()
        self.notifier = notifier
        self.config = config or BrowserWorkerConfig()

    async def run_once(self) -> BrowserWorkerOutcome | None:
        application = await self.repository.claim_browser_application(
            self.config.worker_id, self.config.lease_seconds
        )
        if application is None:
            return None
        return await self.process_claimed(application)

    async def process_claimed(
        self, application: Mapping[str, Any]
    ) -> BrowserWorkerOutcome:
        application_id = int(_record_value(application, "id"))
        vacancy_id = int(_record_value(application, "vacancy_id"))
        vacancy = await self.repository.get_vacancy(vacancy_id)
        if vacancy is None:
            reason = f"vacancy {vacancy_id} referenced by application is missing"
            await self.repository.mark_manual_verification(
                application_id,
                error=reason,
                metadata={"stage": "load_vacancy"},
            )
            return BrowserWorkerOutcome(
                application_id,
                "needs_manual_verification",
                reason,
            )
        session_key = f"application-{application_id}"
        runtime = None
        outcome: BrowserWorkerOutcome | None = None
        submit_started = False
        try:
            raw_url = _record_value(vacancy, "official_apply_url") or _record_value(
                vacancy, "canonical_url"
            )
            if not raw_url:
                return await self._reject(
                    application_id, vacancy_id, "vacancy has no official apply URL"
                )
            try:
                validated_url = await self.url_guard.validate(str(raw_url))
            except URLValidationError as exc:
                return await self._reject(
                    application_id, vacancy_id, f"unsafe apply URL: {exc}"
                )

            runtime = await self.runtime_factory.create(session_key=session_key)
            adapter = GenericFormAdapter(runtime)
            await adapter.navigate(validated_url.url)
            final_url = await adapter.current_url()
            try:
                await self.url_guard.validate(final_url)
            except URLValidationError as exc:
                return await self._reject(
                    application_id, vacancy_id, f"unsafe redirect URL: {exc}"
                )

            captcha_outcome = await self._checkpoint_if_captcha(
                application_id=application_id,
                vacancy=vacancy,
                adapter=adapter,
            )
            if captcha_outcome is not None:
                outcome = captcha_outcome
                return outcome

            snapshot = await adapter.snapshot(
                max_text_chars=self.config.max_page_text_chars
            )
            try:
                evaluation = await self.evaluator.evaluate(snapshot)
            except PageEvaluationError as exc:
                await self.repository.mark_manual_verification(
                    application_id,
                    error=str(exc),
                    metadata={"stage": "page_evaluation"},
                )
                return BrowserWorkerOutcome(
                    application_id, "needs_manual_verification", str(exc)
                )

            stop = await self._apply_page_verdict(
                application_id, vacancy_id, vacancy, evaluation
            )
            if stop is not None:
                return stop

            resume_path, resume_sha = await self._validate_resume(application)
            if resume_path is None or resume_sha is None:
                reason = "resume path or SHA-256 does not match the exact allowlist"
                await self.repository.mark_manual_verification(
                    application_id,
                    error=reason,
                    metadata={"stage": "resume_validation"},
                )
                return BrowserWorkerOutcome(
                    application_id, "needs_manual_verification", reason
                )

            fields = await adapter.fields()
            if not fields:
                return await self._needs_human(
                    application_id,
                    "no application form fields were found",
                    {"page_url": final_url},
                )
            try:
                mapping = await self.mapper.map_fields(fields, self.profile)
            except FieldMappingError as exc:
                await self.repository.mark_manual_verification(
                    application_id,
                    error=str(exc),
                    metadata={"stage": "form_mapping"},
                )
                return BrowserWorkerOutcome(
                    application_id, "needs_manual_verification", str(exc)
                )
            if mapping.unknown_required or len(mapping.resume_fields) != 1:
                return await self._unknown_fields(application_id, mapping)

            try:
                expected = await adapter.fill(mapping, resume_path=resume_path)
                readback = await adapter.verify_readback(expected)
            except FormInteractionError as exc:
                await self.repository.mark_manual_verification(
                    application_id,
                    error=str(exc),
                    metadata={"stage": "form_fill"},
                )
                return BrowserWorkerOutcome(
                    application_id, "needs_manual_verification", str(exc)
                )
            if not readback.matches:
                reason = "form read-back mismatch; refusing to approve or submit"
                await self.repository.mark_manual_verification(
                    application_id,
                    error=reason,
                    metadata={
                        "stage": "form_readback",
                        "mismatched_refs": list(readback.mismatched_refs),
                    },
                )
                return BrowserWorkerOutcome(
                    application_id, "needs_manual_verification", reason
                )

            before_path = self._artifact_path(application_id, "before-submit.png")
            await adapter.screenshot(before_path)
            if not before_path.is_file():
                raise RuntimeError("browser did not create before-submit screenshot")
            payload = self._build_form_payload(
                application_id=application_id,
                page_url=final_url,
                mapping=mapping,
                readback=readback.values,
                resume_sha256=resume_sha,
            )
            payload_hash = canonical_json_hash(payload)

            if not self._has_exact_approval(
                application, payload_hash=payload_hash, resume_sha256=resume_sha
            ):
                await self.repository.update_prepared_application(
                    application_id,
                    form_payload=payload,
                    form_payload_hash=payload_hash,
                    resume_sha256=resume_sha,
                    metadata={
                        "page_evaluation": evaluation.model_dump(mode="json"),
                        "before_screenshot_path": str(before_path),
                        "rendered_form_summary": payload,
                    },
                )
                return BrowserWorkerOutcome(
                    application_id,
                    "ready_for_submit_approval",
                    "form prepared; exact payload and resume approval required",
                )

            if not self.config.final_submit_enabled:
                await self.repository.transition_application(
                    application_id,
                    ApplicationState.READY_FOR_SUBMIT_APPROVAL.value,
                    actor=self.config.worker_id,
                    event_type="final_submit_disabled",
                    metadata={
                        "payload_hash": payload_hash,
                        "resume_sha256": resume_sha,
                        "before_screenshot_path": str(before_path),
                    },
                    expected_states=[ApplicationState.PROCESSING_BROWSER.value],
                )
                return BrowserWorkerOutcome(
                    application_id,
                    "ready_for_submit_approval",
                    "exact approval is valid, but final submit is disabled",
                )

            captcha_outcome = await self._checkpoint_if_captcha(
                application_id=application_id,
                vacancy=vacancy,
                adapter=adapter,
            )
            if captcha_outcome is not None:
                outcome = captcha_outcome
                return outcome

            # Close the resume TOCTOU window immediately before entering the
            # one-way submitting state.
            if _sha256_file(resume_path) != resume_sha:
                reason = "resume changed after preparation; a new approval is required"
                await self.repository.mark_manual_verification(
                    application_id,
                    error=reason,
                    metadata={"stage": "pre_submit_resume_recheck"},
                )
                return BrowserWorkerOutcome(
                    application_id, "needs_manual_verification", reason
                )

            await self.repository.transition_application(
                application_id,
                ApplicationState.SUBMITTING.value,
                actor=self.config.worker_id,
                event_type="submit_started",
                metadata={
                    "payload_hash": payload_hash,
                    "resume_sha256": resume_sha,
                },
                expected_states=[ApplicationState.PROCESSING_BROWSER.value],
            )
            submit_started = True
            try:
                observation = await adapter.submit_once()
                if not observation.clicked:
                    raise RuntimeError("adapter returned without a submit click")
                if not observation.business_confirmed:
                    raise RuntimeError(
                        "submit click had no explicit business confirmation"
                    )
                after_path = self._artifact_path(application_id, "after-submit.png")
                await adapter.screenshot(after_path)
                if not after_path.is_file():
                    raise RuntimeError("browser did not create after-submit screenshot")
                proof = {
                    "application_id": application_id,
                    "before_screenshot_sha256": _sha256_file(before_path),
                    "after_screenshot_sha256": _sha256_file(after_path),
                    "confirmation_text": observation.confirmation_text,
                    "confirmation_url": observation.confirmation_url,
                    "provider_application_id": observation.provider_application_id,
                    "uploaded_resume_sha256": resume_sha,
                    "submitted_payload_hash": payload_hash,
                }
                await self.repository.record_submission_proof_and_mark_submitted(
                    application_id,
                    before_screenshot_path=str(before_path),
                    after_screenshot_path=str(after_path),
                    confirmation_text=observation.confirmation_text,
                    confirmation_url=observation.confirmation_url,
                    provider_application_id=observation.provider_application_id,
                    uploaded_resume_sha256=resume_sha,
                    submitted_payload_hash=payload_hash,
                    proof_hash=canonical_json_hash(proof),
                )
                return BrowserWorkerOutcome(
                    application_id,
                    "submitted",
                    "business confirmation and proof recorded atomically",
                )
            except Exception as exc:
                # The click may have reached the provider.  Never release this
                # application back to the automatic queue and never click again.
                refreshed = await self.repository.get_application(application_id)
                if bool(_record_value(refreshed, "is_submitted", False)):
                    return BrowserWorkerOutcome(
                        application_id,
                        "submitted",
                        "submission was committed despite a lost worker response",
                    )
                await self.repository.mark_manual_verification(
                    application_id,
                    error=f"ambiguous submit outcome: {exc}",
                    metadata={"stage": "after_submit_started", "never_retry": True},
                )
                return BrowserWorkerOutcome(
                    application_id,
                    "needs_manual_verification",
                    f"ambiguous submit outcome: {exc}",
                )
        except Exception as exc:
            if submit_started:
                await self.repository.mark_manual_verification(
                    application_id,
                    error=f"ambiguous submit outcome: {exc}",
                    metadata={"stage": "after_submit_started", "never_retry": True},
                )
                return BrowserWorkerOutcome(
                    application_id,
                    "needs_manual_verification",
                    f"ambiguous submit outcome: {exc}",
                )
            await self.repository.release_application(
                application_id,
                self.config.worker_id,
                error=f"browser processing failed before submit: {exc}",
            )
            return BrowserWorkerOutcome(
                application_id,
                "retryable_error",
                f"browser processing failed before submit: {exc}",
            )
        finally:
            if runtime is not None and not (
                outcome is not None
                and outcome.keep_session_open
                and self.config.keep_context_open_on_captcha
            ):
                await runtime.close(preserve_session=True)

    async def _validate_resume(
        self, application: Mapping[str, Any]
    ) -> tuple[Path | None, str | None]:
        raw_path = _record_value(application, "resume_path")
        expected_sha = _record_value(application, "resume_sha256")
        if not raw_path or not expected_sha:
            return None, None
        path = Path(str(raw_path))
        try:
            if path.is_symlink() or not path.is_file():
                return None, None
            if not self.profile.is_resume_allowed(path):
                return None, None
            digest = _sha256_file(path)
        except (OSError, ValueError):
            return None, None
        if digest != str(expected_sha):
            return None, None
        return path.resolve(strict=True), digest

    async def _apply_page_verdict(
        self,
        application_id: int,
        vacancy_id: int,
        vacancy: Mapping[str, Any],
        evaluation: PageEvaluation,
    ) -> BrowserWorkerOutcome | None:
        company = str(_record_value(vacancy, "company", "") or "").casefold()
        if "иннотех" in company or "innotech" in company:
            return await self._needs_human(
                application_id,
                "Innotech applications are never processed automatically",
                {"page_evaluation": evaluation.model_dump(mode="json")},
            )
        rejection_reason = None
        if not evaluation.active:
            rejection_reason = f"page says vacancy is closed: {evaluation.reason}"
        elif evaluation.verdict == "garbage":
            rejection_reason = f"page evaluator rejected vacancy: {evaluation.reason}"
        elif evaluation.target_role not in {"java_backend", "go_backend"}:
            rejection_reason = f"page role is not Java/Go backend: {evaluation.reason}"
        elif evaluation.remote_type != "remote_only":
            rejection_reason = f"page role is not fully remote: {evaluation.reason}"
        elif (
            evaluation.salary_max_rub is not None
            and evaluation.salary_max_rub < 300_000
        ):
            rejection_reason = "page salary maximum is below 300000 RUB"
        if rejection_reason is not None:
            return await self._reject(application_id, vacancy_id, rejection_reason)
        if evaluation.verdict != "qualified":
            return await self._needs_human(
                application_id,
                f"page evaluator requires review: {evaluation.reason}",
                {"page_evaluation": evaluation.model_dump(mode="json")},
            )
        return None

    async def _checkpoint_if_captcha(
        self,
        *,
        application_id: int,
        vacancy: Mapping[str, Any],
        adapter: GenericFormAdapter,
    ) -> BrowserWorkerOutcome | None:
        evidence = await adapter.captcha()
        if not evidence.detected:
            return None
        current_url = await adapter.current_url()
        screenshot = self._artifact_path(application_id, "captcha.png")
        await adapter.screenshot(screenshot)
        await self.repository.create_captcha_checkpoint(
            application_id,
            page_url=current_url,
            screenshot_path=str(screenshot),
            persistent_session_key=adapter.session_key,
            instructions=self.config.captcha_instructions,
        )
        payload = captcha_notification_payload(
            application_id=application_id,
            company=_record_value(vacancy, "company"),
            title=_record_value(vacancy, "title"),
            url=current_url,
            screenshot_path=str(screenshot),
            instructions=self.config.captcha_instructions,
        )
        if self.notifier is not None:
            try:
                await self.notifier.notify(payload)
            except Exception:
                # The durable DB checkpoint is authoritative; an optional
                # notification transport must never cause CAPTCHA bypass/retry.
                pass
        return BrowserWorkerOutcome(
            application_id,
            "captcha_required",
            f"human checkpoint created ({', '.join(evidence.markers)})",
            keep_session_open=True,
        )

    async def _reject(
        self, application_id: int, vacancy_id: int, reason: str
    ) -> BrowserWorkerOutcome:
        await self.repository.reject_vacancy_for_browser(
            vacancy_id, application_id, reason=reason
        )
        return BrowserWorkerOutcome(application_id, "rejected", reason)

    async def _needs_human(
        self,
        application_id: int,
        reason: str,
        metadata: Mapping[str, Any],
    ) -> BrowserWorkerOutcome:
        await self.repository.transition_application(
            application_id,
            ApplicationState.NEEDS_HUMAN_ANSWER.value,
            actor=self.config.worker_id,
            event_type="browser_human_input_required",
            metadata=metadata,
            last_error=reason,
            expected_states=[ApplicationState.PROCESSING_BROWSER.value],
        )
        return BrowserWorkerOutcome(application_id, "needs_human_answer", reason)

    async def _unknown_fields(
        self, application_id: int, mapping: FormMapping
    ) -> BrowserWorkerOutcome:
        metadata = {
            "unknown_required_fields": [
                field.metadata() for field in mapping.unknown_required
            ],
            "resume_field_count": len(mapping.resume_fields),
        }
        return await self._needs_human(
            application_id,
            "unknown required form field or no unique resume field",
            metadata,
        )

    @staticmethod
    def _build_form_payload(
        *,
        application_id: int,
        page_url: str,
        mapping: FormMapping,
        readback: Mapping[str, object],
        resume_sha256: str,
    ) -> dict[str, Any]:
        fields = []
        for mapped in sorted(mapping.mapped_fields, key=lambda item: item.field.ref):
            fields.append(
                {
                    "ref": mapped.field.ref,
                    "name": mapped.field.name,
                    "label": mapped.field.label,
                    "input_type": mapped.field.input_type,
                    "profile_key": mapped.profile_key,
                    "value": readback.get(mapped.field.ref),
                }
            )
        return {
            "schema_version": 1,
            "application_id": application_id,
            "page_url": page_url,
            "fields": fields,
            "resume_sha256": resume_sha256,
        }

    @staticmethod
    def _has_exact_approval(
        application: Mapping[str, Any], *, payload_hash: str, resume_sha256: str
    ) -> bool:
        return bool(
            _record_value(application, "approved_at")
            and _record_value(application, "approved_payload_hash") == payload_hash
            and _record_value(application, "form_payload_hash") == payload_hash
            and _record_value(application, "approved_resume_sha256") == resume_sha256
            and _record_value(application, "resume_sha256") == resume_sha256
        )

    def _artifact_path(self, application_id: int, suffix: str) -> Path:
        root = self.config.artifacts_dir.resolve()
        root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
        safe_suffix = "".join(
            character
            for character in suffix
            if character.isascii() and (character.isalnum() or character in ".-_")
        )
        if safe_suffix != suffix or not safe_suffix:
            raise ValueError("unsafe artifact suffix")
        return root / f"application-{application_id}-{safe_suffix}"
