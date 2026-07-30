from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from browser_worker.adapters.base import (
    CaptchaPageMetadata,
    FormField,
    SubmitObservation,
)
from browser_worker.evaluator import PageEvaluation, PageSnapshot
from browser_worker.form_mapper import CandidateProfile
from browser_worker.security import ValidatedURL
from browser_worker.worker import BrowserWorker, BrowserWorkerConfig


class FakeGuard:
    async def validate(
        self, url: str, *, enforce_domain_allowlist: bool = True
    ) -> ValidatedURL:
        del enforce_domain_allowlist
        return ValidatedURL(
            url=url,
            hostname="jobs.example.test",
            addresses=("203.0.113.1",),
        )


class FakeEvaluator:
    async def evaluate(self, snapshot: PageSnapshot) -> PageEvaluation:
        return PageEvaluation.model_validate(
            {
                "active": True,
                "target_role": "java_backend",
                "remote_type": "remote_only",
                "salary_min_rub": 350_000,
                "salary_max_rub": 450_000,
                "verdict": "qualified",
                "confidence": 0.95,
                "reason": "active remote Java backend vacancy",
                "risks": [],
            }
        )


class FakeRuntime:
    def __init__(self, session_key: str, *, captcha: bool = False, submit_error: Exception | None = None):
        self.session_key = session_key
        self.captcha_enabled = captcha
        self.submit_error = submit_error
        self.values: dict[str, object] = {}
        self.submit_calls = 0
        self.closed = False

    async def navigate(self, url: str) -> None:
        self.url = url

    async def current_url(self) -> str:
        return self.url

    async def snapshot(self, *, max_text_chars: int) -> PageSnapshot:
        del max_text_chars
        return PageSnapshot(
            url=self.url,
            title="Senior Java Developer",
            main_text="Remote Java backend role, salary 400000 RUB",
            body_text="",
        )

    async def extract_form_fields(self):
        return (
            FormField("email", "Email", "email", required=True),
            FormField("resume", "Resume / CV", "file", required=True),
        )

    async def captcha_metadata(self, *, selectors):
        del selectors
        if self.captcha_enabled:
            return CaptchaPageMetadata(
                iframe_sources=("https://www.google.com/recaptcha/api2/anchor",),
                visible_text="verify you are human",
            )
        return CaptchaPageMetadata()

    async def fill_text(self, field_ref: str, value: str) -> None:
        self.values[field_ref] = value

    async def select_option(self, field_ref: str, value: str) -> None:
        self.values[field_ref] = value

    async def set_checked(self, field_ref: str, checked: bool) -> None:
        self.values[field_ref] = checked

    async def upload_file(self, field_ref: str, path: Path) -> None:
        assert field_ref == "resume"
        assert path.is_file()

    async def read_form_values(self) -> dict[str, object]:
        return dict(self.values)

    async def screenshot(self, path: Path) -> None:
        path.write_bytes(b"fake-png")

    async def submit_once(self) -> SubmitObservation:
        self.submit_calls += 1
        if self.submit_error is not None:
            raise self.submit_error
        return SubmitObservation(
            clicked=True,
            business_confirmed=True,
            confirmation_text="Application received",
            confirmation_url="https://jobs.example.test/thanks",
        )

    async def close(self, *, preserve_session: bool) -> None:
        del preserve_session
        self.closed = True


class FakeRuntimeFactory:
    def __init__(self, *, captcha: bool = False, submit_error: Exception | None = None):
        self.captcha = captcha
        self.submit_error = submit_error
        self.created: list[FakeRuntime] = []

    async def create(self, *, session_key: str) -> FakeRuntime:
        runtime = FakeRuntime(
            session_key,
            captcha=self.captcha,
            submit_error=self.submit_error,
        )
        self.created.append(runtime)
        return runtime


class FakeRepository:
    def __init__(self, app: dict, vacancy: dict | None):
        self.app = app
        self.vacancy = vacancy
        self.manual_errors: list[str] = []
        self.checkpoints: list[dict] = []
        self.prepared: list[dict] = []
        self.transitions: list[str] = []
        self.releases = 0

    async def claim_browser_application(
        self, worker_id: str, lease_seconds: int = 300
    ):
        del worker_id, lease_seconds
        return self.app

    async def get_vacancy(self, vacancy_id: int):
        del vacancy_id
        return self.vacancy

    async def get_application(self, application_id: int):
        assert application_id == self.app["id"]
        return self.app

    async def mark_manual_verification(self, application_id: int, *, error: str, actor="browser_worker", metadata=None):
        del application_id, actor, metadata
        self.manual_errors.append(error)
        self.app["state"] = "needs_manual_verification"
        return self.app

    async def create_captcha_checkpoint(self, application_id: int, **values):
        checkpoint = {"id": len(self.checkpoints) + 1, "application_id": application_id, **values}
        self.checkpoints.append(checkpoint)
        self.app["state"] = "captcha_required"
        return checkpoint

    async def update_prepared_application(self, application_id: int, *, form_payload, form_payload_hash, resume_sha256=None, **kwargs):
        del application_id, kwargs
        self.app.update(
            form_payload=form_payload,
            form_payload_hash=form_payload_hash,
            resume_sha256=resume_sha256 or self.app["resume_sha256"],
            state="ready_for_submit_approval",
        )
        self.prepared.append(dict(self.app))
        return self.app

    async def transition_application(self, application_id: int, new_state: str, **kwargs):
        del application_id, kwargs
        self.transitions.append(new_state)
        self.app["state"] = new_state
        return self.app

    async def release_application(self, application_id: int, worker_id: str, **kwargs):
        del application_id, worker_id, kwargs
        self.releases += 1
        return self.app

    async def reject_vacancy_for_browser(self, vacancy_id: int, application_id: int, **kwargs):
        del vacancy_id, application_id, kwargs
        self.app["state"] = "rejected"
        return self.app

    async def record_submission_proof_and_mark_submitted(self, application_id: int, **kwargs):
        del application_id, kwargs
        self.app.update(state="submitted", is_submitted=True)
        return self.app


def make_worker(tmp_path: Path, repository: FakeRepository, factory: FakeRuntimeFactory, resume: Path, *, submit=False):
    return BrowserWorker(
        repository=repository,
        runtime_factory=factory,
        evaluator=FakeEvaluator(),
        profile=CandidateProfile(
            values={"email": "nazar@example.test"},
            allowed_resume_paths=(resume,),
        ),
        url_guard=FakeGuard(),
        config=BrowserWorkerConfig(
            final_submit_enabled=submit,
            artifacts_dir=tmp_path / "artifacts",
        ),
    )


def application(resume: Path) -> dict:
    return {
        "id": 7,
        "vacancy_id": 9,
        "state": "processing_browser",
        "resume_path": str(resume),
        "resume_sha256": hashlib.sha256(resume.read_bytes()).hexdigest(),
        "form_payload_hash": "not-prepared",
        "approved_at": None,
        "approved_payload_hash": None,
        "approved_resume_sha256": None,
        "is_submitted": False,
    }


def vacancy() -> dict:
    return {
        "id": 9,
        "company": "Example",
        "title": "Senior Java Developer",
        "canonical_url": "https://jobs.example.test/7",
        "official_apply_url": "https://jobs.example.test/7/apply",
    }


def test_missing_vacancy_fails_closed_without_opening_browser(tmp_path: Path) -> None:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"resume")
    repo = FakeRepository(application(resume), None)
    factory = FakeRuntimeFactory()
    worker = make_worker(tmp_path, repo, factory, resume)

    outcome = asyncio.run(worker.process_claimed(repo.app))

    assert outcome.status == "needs_manual_verification"
    assert repo.manual_errors and "vacancy 9" in repo.manual_errors[0]
    assert factory.created == []


def test_captcha_creates_human_checkpoint_and_never_submits(tmp_path: Path) -> None:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"resume")
    repo = FakeRepository(application(resume), vacancy())
    factory = FakeRuntimeFactory(captcha=True)
    worker = make_worker(tmp_path, repo, factory, resume, submit=True)

    outcome = asyncio.run(worker.process_claimed(repo.app))

    assert outcome.status == "captcha_required"
    assert outcome.keep_session_open is True
    assert len(repo.checkpoints) == 1
    assert factory.created[0].submit_calls == 0
    assert factory.created[0].closed is False


def test_unapproved_payload_is_prepared_but_not_submitted(tmp_path: Path) -> None:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"resume")
    repo = FakeRepository(application(resume), vacancy())
    factory = FakeRuntimeFactory()
    worker = make_worker(tmp_path, repo, factory, resume, submit=True)

    outcome = asyncio.run(worker.process_claimed(repo.app))

    assert outcome.status == "ready_for_submit_approval"
    assert len(repo.prepared) == 1
    assert repo.app["form_payload_hash"]
    assert factory.created[0].submit_calls == 0


def test_ambiguous_submit_is_never_retried_automatically(tmp_path: Path) -> None:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"resume")
    repo = FakeRepository(application(resume), vacancy())
    prepare_factory = FakeRuntimeFactory()
    prepare_worker = make_worker(tmp_path, repo, prepare_factory, resume, submit=True)
    first = asyncio.run(prepare_worker.process_claimed(repo.app))
    assert first.status == "ready_for_submit_approval"

    repo.app.update(
        state="processing_browser",
        approved_at="2026-01-01T00:00:00Z",
        approved_payload_hash=repo.app["form_payload_hash"],
        approved_resume_sha256=repo.app["resume_sha256"],
    )
    failing_factory = FakeRuntimeFactory(submit_error=TimeoutError("response lost after click"))
    submit_worker = make_worker(tmp_path, repo, failing_factory, resume, submit=True)

    outcome = asyncio.run(submit_worker.process_claimed(repo.app))

    assert outcome.status == "needs_manual_verification"
    assert failing_factory.created[0].submit_calls == 1
    assert repo.releases == 0
    assert repo.app["state"] == "needs_manual_verification"
    assert any("ambiguous submit outcome" in error for error in repo.manual_errors)
