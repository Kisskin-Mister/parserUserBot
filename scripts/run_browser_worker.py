#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Make direct ``python scripts/run_browser_worker.py`` work from the repository.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from browser_worker.adapters.playwright import PlaywrightRuntimeFactory  # noqa: E402
from browser_worker.evaluator import OpenAIPageEvaluator  # noqa: E402
from browser_worker.form_mapper import (  # noqa: E402
    CandidateProfile,
    DeterministicFormMapper,
)
from browser_worker.security import URLGuard  # noqa: E402
from browser_worker.worker import BrowserWorker, BrowserWorkerConfig  # noqa: E402
from database import Database  # noqa: E402
from vacancy_pipeline.repository import PipelineRepository  # noqa: E402


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    value = int(os.getenv(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _load_profile(path: Path) -> CandidateProfile:
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read candidate profile: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("candidate profile must be a JSON object")
    values = payload.get("values")
    resume_paths = payload.get("allowed_resume_paths")
    if not isinstance(values, dict) or not isinstance(resume_paths, list):
        raise ValueError(
            "candidate profile requires object `values` and list "
            "`allowed_resume_paths`"
        )
    base = path.resolve().parent
    allowed_paths = tuple(
        candidate if candidate.is_absolute() else base / candidate
        for candidate in (Path(str(item)) for item in resume_paths)
    )
    return CandidateProfile(values=values, allowed_resume_paths=allowed_paths)


async def run() -> None:
    load_dotenv()
    profile_path_raw = os.getenv("BROWSER_CANDIDATE_PROFILE_PATH")
    if not profile_path_raw:
        raise ValueError("BROWSER_CANDIDATE_PROFILE_PATH is required")
    profile = _load_profile(Path(profile_path_raw))

    final_submit_enabled = _bool_env("BROWSER_FINAL_SUBMIT_ENABLED", False)
    if final_submit_enabled and os.getenv("BROWSER_FINAL_SUBMIT_ACK") != (
        "SUBMIT_APPROVED_APPLICATIONS"
    ):
        raise ValueError(
            "final submission requires "
            "BROWSER_FINAL_SUBMIT_ACK=SUBMIT_APPROVED_APPLICATIONS"
        )

    allowed_domains = tuple(
        item.strip()
        for item in os.getenv("BROWSER_ALLOWED_DOMAINS", "").split(",")
        if item.strip()
    )
    guard = URLGuard(allowed_domains=allowed_domains)
    runtime_factory = PlaywrightRuntimeFactory(
        user_data_root=Path(
            os.getenv("BROWSER_USER_DATA_DIR", "runtime/browser_profiles")
        ),
        guard=guard,
        headless=_bool_env("BROWSER_HEADLESS", True),
    )
    evaluator = OpenAIPageEvaluator(
        base_url=os.environ["OPENAI_BASE_URL"],
        api_key=os.environ["OPENAI_API_KEY"],
        model=os.environ["OPENAI_MODEL"],
    )

    database = Database()
    await database.init()
    repository = PipelineRepository(database.pool)
    config = BrowserWorkerConfig(
        worker_id=os.getenv("BROWSER_WORKER_ID", "browser-worker"),
        lease_seconds=_int_env(
            "BROWSER_LEASE_SECONDS", 300, minimum=30, maximum=3600
        ),
        final_submit_enabled=final_submit_enabled,
        artifacts_dir=Path(
            os.getenv("BROWSER_ARTIFACTS_DIR", "runtime/browser_worker")
        ),
        max_page_text_chars=_int_env(
            "BROWSER_MAX_PAGE_TEXT_CHARS",
            24_000,
            minimum=1_000,
            maximum=100_000,
        ),
    )
    worker = BrowserWorker(
        repository=repository,
        runtime_factory=runtime_factory,
        evaluator=evaluator,
        profile=profile,
        url_guard=guard,
        mapper=DeterministicFormMapper(),
        config=config,
    )
    poll_seconds = _int_env(
        "BROWSER_POLL_SECONDS", 5, minimum=1, maximum=60
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, stop.set)
        except NotImplementedError:
            pass

    logging.info(
        "browser worker started (final submit enabled=%s)", final_submit_enabled
    )
    try:
        while not stop.is_set():
            outcome = await worker.run_once()
            if outcome is None:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
                except TimeoutError:
                    pass
                continue
            logging.info(
                "browser application_id=%s status=%s",
                outcome.application_id,
                outcome.status,
            )
    finally:
        await runtime_factory.shutdown()
        if database.pool is not None:
            await database.pool.close()


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
