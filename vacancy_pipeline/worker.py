from __future__ import annotations

import asyncio
import logging
import os
import socket

from vacancy_pipeline.service import VacancyPipelineService


logger = logging.getLogger(__name__)


class VacancyWorker:
    def __init__(self, service: VacancyPipelineService, *, worker_id: str | None = None, poll_seconds: float = 3.0) -> None:
        self.service = service
        self.worker_id = worker_id or f"vacancy:{socket.gethostname()}:{os.getpid()}"
        self.poll_seconds = poll_seconds

    async def run_once(self):
        return await self.service.process_batch(self.worker_id)

    async def run_forever(self) -> None:
        while True:
            result = await self.run_once()
            if result.claimed == 0:
                await asyncio.sleep(self.poll_seconds)
            elif result.failed:
                logger.warning("vacancy batch had %s failed intake rows", result.failed)
