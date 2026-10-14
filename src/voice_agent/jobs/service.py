"""Accepting commands as durable jobs.

``submit`` commits the job to SQLite *before* returning, so the assistant only
acknowledges a command once it can survive a hang-up or a process crash.
Enqueueing to Redis is best-effort: if it fails, the worker's reconciler finds
the queued job in SQLite and enqueues it later.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from voice_agent.jobs.models import Job, JobSpec
from voice_agent.jobs.queue import JobQueue
from voice_agent.jobs.store import JobStore
from voice_agent.logging_setup import get_logger

log = get_logger(__name__)


def make_idempotency_key(session_id: str, turn: int, normalized_text: str) -> str:
    """Stable key for one accepted voice command (same call + turn + words)."""
    raw = f"{session_id}\x1f{turn}\x1f{normalized_text}".encode()
    return "vc_" + hashlib.sha256(raw).hexdigest()[:40]


class JobService:
    def __init__(
        self,
        store: JobStore,
        queue: JobQueue,
        *,
        max_attempts: int,
        enqueue_timeout: float = 2.0,
    ):
        self.store = store
        self.queue = queue
        self.max_attempts = max_attempts
        self.enqueue_timeout = enqueue_timeout

    async def submit(
        self,
        spec: JobSpec,
        *,
        idempotency_key: str,
        transcript: str,
        intent: dict[str, Any],
        session_id: str | None = None,
        call_id: str | None = None,
    ) -> tuple[Job, bool]:
        job, created = await asyncio.to_thread(
            self.store.create,
            spec,
            idempotency_key=idempotency_key,
            transcript=transcript,
            intent=intent,
            max_attempts=self.max_attempts,
            session_id=session_id,
            call_id=call_id,
        )
        if created:
            log.info("job.accepted", job_id=job.id, connector=spec.connector, session_id=session_id)
            try:
                await asyncio.wait_for(self.queue.enqueue(job.id), self.enqueue_timeout)
            except Exception as exc:
                # Job is safe in SQLite; the reconciler will enqueue it.
                log.warning("job.enqueue_deferred", job_id=job.id, error=repr(exc))
        else:
            log.info("job.duplicate_submission", job_id=job.id, status=job.status.value)
        return job, created
