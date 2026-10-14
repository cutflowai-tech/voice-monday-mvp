"""Background worker: executes durable jobs through connectors.

Completely independent of any phone call: it only reads job ids from the queue
and job state from SQLite. A caller hanging up has no effect on it.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from dataclasses import dataclass

from voice_agent.connectors.base import (
    ConnectorError,
    ConnectorRegistry,
    ExecutionContext,
)
from voice_agent.jobs.models import Job
from voice_agent.jobs.queue import JobQueue
from voice_agent.jobs.store import JobStore
from voice_agent.logging_setup import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class RetryPolicy:
    base_seconds: float = 2.0
    max_seconds: float = 120.0
    jitter: float = 0.25

    def delay(self, attempt: int, retry_after: float | None = None) -> float:
        """Exponential backoff: base * 2^(attempt-1), +-jitter, honoring Retry-After."""
        delay = min(self.max_seconds, self.base_seconds * (2 ** max(0, attempt - 1)))
        delay *= 1 + random.uniform(-self.jitter, self.jitter)
        if retry_after is not None:
            delay = max(delay, min(retry_after, self.max_seconds * 5))
        return max(0.0, delay)


class Worker:
    def __init__(
        self,
        *,
        store: JobStore,
        queue: JobQueue,
        connectors: ConnectorRegistry,
        retry: RetryPolicy,
        execution_timeout: float = 30.0,
        lease_seconds: float = 90.0,
        reconcile_interval: float = 15.0,
        poll_timeout: float = 1.0,
    ):
        self.store = store
        self.queue = queue
        self.connectors = connectors
        self.retry = retry
        self.execution_timeout = execution_timeout
        self.lease_seconds = max(lease_seconds, execution_timeout + 5)
        self.reconcile_interval = reconcile_interval
        self.poll_timeout = poll_timeout
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        recovered = await self.queue.recover_inflight()
        log.info("worker.started", recovered_inflight=recovered)
        reconciler = asyncio.create_task(self._reconcile_loop(), name="job-reconciler")
        try:
            while not self._stop.is_set():
                try:
                    job_id = await self.queue.dequeue(self.poll_timeout)
                except Exception:
                    log.exception("worker.dequeue_failed")
                    await self._sleep(2)
                    continue
                if job_id is None:
                    continue
                # stop() only sets a flag, so the current job always finishes first.
                await self._handle(job_id)
        finally:
            reconciler.cancel()
            await asyncio.gather(reconciler, return_exceptions=True)
            log.info("worker.stopped")

    async def run_until_idle(self, max_wait: float = 30.0) -> None:
        """Process jobs until the queue stays empty (simulator/tests helper)."""
        deadline = asyncio.get_running_loop().time() + max_wait
        while asyncio.get_running_loop().time() < deadline:
            job_id = await self.queue.dequeue(0.05)
            if job_id is None:
                pending = getattr(self.queue, "pending", lambda: 0)()
                if pending == 0:
                    return
                continue
            await self._handle(job_id)

    async def _handle(self, job_id: str) -> None:
        try:
            await self.process(job_id)
        except Exception:
            # Never ack on unexpected errors without state: reconciler will retry it.
            log.exception("worker.process_crashed", job_id=job_id)
        finally:
            try:
                await self.queue.ack(job_id)
            except Exception:
                log.exception("worker.ack_failed", job_id=job_id)

    async def process(self, job_id: str) -> Job | None:
        job = await asyncio.to_thread(self.store.claim, job_id, self.lease_seconds)
        if job is None:
            log.debug("worker.skip_not_claimable", job_id=job_id)
            return None
        log.info(
            "job.processing",
            job_id=job.id,
            attempt=job.attempts,
            connector=job.connector,
            operation=job.operation,
        )
        ctx = ExecutionContext(
            job_id=job.id,
            idempotency_key=job.idempotency_key,
            attempt=job.attempts,
            created_at=job.created_at,
            prior_attempt_uncertain=job.prior_attempt_uncertain,
        )
        try:
            connector = self.connectors.get(job.connector)
            result = await asyncio.wait_for(
                connector.execute(job.operation, job.payload, ctx), self.execution_timeout
            )
        except ConnectorError as exc:
            return await self._on_error(job, exc, exc.retryable, exc.retry_after, exc.uncertain)
        except TimeoutError:
            exc = TimeoutError(f"execution exceeded {self.execution_timeout}s")
            return await self._on_error(job, exc, True, None, True)
        except KeyError as exc:
            return await self._on_error(job, exc, False, None, False)
        except Exception as exc:
            log.exception("job.unexpected_error", job_id=job.id)
            return await self._on_error(job, exc, True, None, True)

        updated = await asyncio.to_thread(
            self.store.complete, job, result=result.data, external_ref=result.external_id
        )
        log.info("job.completed", job_id=job.id, external_ref=result.external_id)
        return updated

    async def _on_error(
        self,
        job: Job,
        exc: BaseException,
        retryable: bool,
        retry_after: float | None,
        uncertain: bool,
    ) -> Job | None:
        error = f"{type(exc).__name__}: {exc}"[:2000]
        if not retryable:
            log.error("job.failed", job_id=job.id, attempt=job.attempts, error=error)
            return await asyncio.to_thread(self.store.fail, job, error=error)
        if job.attempts >= job.max_attempts:
            log.error("job.dead_letter", job_id=job.id, attempts=job.attempts, error=error)
            return await asyncio.to_thread(self.store.dead_letter, job, error=error)
        delay = self.retry.delay(job.attempts, retry_after)
        log.warning(
            "job.retry_scheduled",
            job_id=job.id,
            attempt=job.attempts,
            delay_seconds=round(delay, 2),
            error=error,
        )
        updated = await asyncio.to_thread(
            self.store.schedule_retry, job, error=error, delay_seconds=delay, uncertain=uncertain
        )
        if updated is not None:
            await self.queue.enqueue(job.id, delay_seconds=delay)
        return updated

    async def _reconcile_loop(self) -> None:
        while not self._stop.is_set():
            await self._sleep(self.reconcile_interval)
            try:
                await self.reconcile()
            except Exception:
                log.exception("worker.reconcile_failed")

    async def reconcile(self) -> int:
        """Re-enqueue due jobs that may have been lost (Redis restart, crash)."""
        ids = await asyncio.to_thread(
            self.store.find_recoverable, stale_after_seconds=self.reconcile_interval
        )
        for job_id in ids:
            await self.queue.enqueue(job_id)
        if ids:
            log.info("worker.reconciled", count=len(ids))
        return len(ids)

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), seconds)
