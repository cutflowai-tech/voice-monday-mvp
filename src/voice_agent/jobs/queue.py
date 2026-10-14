"""JobQueue interface plus Redis and in-memory implementations.

The queue only carries job ids. SQLite holds the authoritative state, so a lost
or duplicated queue message is harmless: duplicates fail the guarded ``claim``
and lost messages are re-enqueued by the worker's reconciler.

Redis layout (reliable-queue pattern)::

    {ns}:ready                 LIST  job ids ready to run (LPUSH / BLMOVE RIGHT)
    {ns}:delayed               ZSET  job id -> unix time when it becomes ready
    {ns}:processing:{worker}   LIST  ids a worker has taken but not yet acked
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod

from redis.asyncio import Redis


class JobQueue(ABC):
    @abstractmethod
    async def enqueue(self, job_id: str, *, delay_seconds: float = 0.0) -> None: ...

    @abstractmethod
    async def dequeue(self, timeout_seconds: float) -> str | None:
        """Reserve the next ready job id, waiting up to ``timeout_seconds``."""

    @abstractmethod
    async def ack(self, job_id: str) -> None:
        """Mark a reserved job id as handled."""

    @abstractmethod
    async def recover_inflight(self) -> int:
        """Return reserved-but-unacked ids (e.g. after a crash) to the ready list."""

    @abstractmethod
    async def ping(self) -> bool: ...

    async def close(self) -> None:  # noqa: B027 - optional hook
        pass


class RedisJobQueue(JobQueue):
    def __init__(self, redis: Redis, *, namespace: str, worker_id: str):
        self.redis = redis
        self.ready_key = f"{namespace}:ready"
        self.delayed_key = f"{namespace}:delayed"
        self.processing_key = f"{namespace}:processing:{worker_id}"

    @classmethod
    def from_url(cls, url: str, *, namespace: str, worker_id: str) -> RedisJobQueue:
        redis = Redis.from_url(
            url, decode_responses=True, socket_timeout=10, socket_connect_timeout=5
        )
        return cls(redis, namespace=namespace, worker_id=worker_id)

    async def enqueue(self, job_id: str, *, delay_seconds: float = 0.0) -> None:
        if delay_seconds > 0:
            await self.redis.zadd(self.delayed_key, {job_id: time.time() + delay_seconds})
        else:
            await self.redis.lpush(self.ready_key, job_id)

    async def _promote_due(self) -> None:
        due = await self.redis.zrangebyscore(self.delayed_key, "-inf", time.time(), 0, 100)
        for job_id in due:
            # Only the client that removes the member promotes it (no double push).
            if await self.redis.zrem(self.delayed_key, job_id):
                await self.redis.lpush(self.ready_key, job_id)

    async def dequeue(self, timeout_seconds: float) -> str | None:
        await self._promote_due()
        return await self.redis.blmove(
            self.ready_key, self.processing_key, timeout_seconds, "RIGHT", "LEFT"
        )

    async def ack(self, job_id: str) -> None:
        await self.redis.lrem(self.processing_key, 0, job_id)

    async def recover_inflight(self) -> int:
        moved = 0
        while await self.redis.lmove(self.processing_key, self.ready_key, "RIGHT", "LEFT"):
            moved += 1
        return moved

    async def ping(self) -> bool:
        return bool(await self.redis.ping())

    async def close(self) -> None:
        await self.redis.aclose()


class InMemoryJobQueue(JobQueue):
    """Single-process queue for the simulator and tests (SQLite stays durable)."""

    def __init__(self) -> None:
        self._ready: asyncio.Queue[str] = asyncio.Queue()
        self._timers: set[asyncio.TimerHandle] = set()
        self.inflight: set[str] = set()

    async def enqueue(self, job_id: str, *, delay_seconds: float = 0.0) -> None:
        if delay_seconds > 0:
            loop = asyncio.get_running_loop()
            handle: asyncio.TimerHandle

            def _release() -> None:
                self._timers.discard(handle)
                self._ready.put_nowait(job_id)

            handle = loop.call_later(delay_seconds, _release)
            self._timers.add(handle)
        else:
            self._ready.put_nowait(job_id)

    async def dequeue(self, timeout_seconds: float) -> str | None:
        try:
            job_id = await asyncio.wait_for(self._ready.get(), timeout_seconds)
        except TimeoutError:
            return None
        self.inflight.add(job_id)
        return job_id

    async def ack(self, job_id: str) -> None:
        self.inflight.discard(job_id)

    async def recover_inflight(self) -> int:
        count = len(self.inflight)
        for job_id in list(self.inflight):
            self._ready.put_nowait(job_id)
        self.inflight.clear()
        return count

    async def ping(self) -> bool:
        return True

    def pending(self) -> int:
        return self._ready.qsize() + len(self._timers)

    async def close(self) -> None:
        for handle in self._timers:
            handle.cancel()
        self._timers.clear()
