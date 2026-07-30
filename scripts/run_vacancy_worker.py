"""Run the PostgreSQL-backed vacancy intake worker.

Use ``python -m scripts.run_vacancy_worker`` from the repository root so the
project modules are importable without modifying ``PYTHONPATH``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

from dotenv import load_dotenv

from database import Database
from vacancy_pipeline.repository import PipelineRepository
from vacancy_pipeline.service import VacancyPipelineService, build_structured_judge_from_env
from vacancy_pipeline.worker import VacancyWorker


logger = logging.getLogger(__name__)


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


async def run(*, once: bool = False) -> None:
    database = Database()
    await database.init()
    if database.pool is None:  # Defensive guard for alternate Database implementations.
        raise RuntimeError("database pool was not initialized")

    repository = PipelineRepository(database.pool)
    service = VacancyPipelineService(
        repository,
        build_structured_judge_from_env(),
        ai_max_calls_per_batch=_positive_int("AI_MAX_CALLS_PER_BATCH", 20),
        lease_seconds=_positive_int("VACANCY_WORKER_LEASE_SECONDS", 300),
    )
    worker = VacancyWorker(
        service,
        worker_id=os.getenv("VACANCY_WORKER_ID") or None,
        poll_seconds=_positive_float("VACANCY_WORKER_POLL_SECONDS", 3.0),
    )

    try:
        if once:
            result = await worker.run_once()
            logger.info(
                "vacancy batch complete: claimed=%s processed=%s failed=%s",
                result.claimed,
                result.processed,
                result.failed,
            )
            return
        await worker.run_forever()
    finally:
        await database.pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="process one claimed batch and exit")
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_dotenv()
    try:
        asyncio.run(run(once=args.once))
    except KeyboardInterrupt:
        logger.info("vacancy worker stopped")


if __name__ == "__main__":
    main()
