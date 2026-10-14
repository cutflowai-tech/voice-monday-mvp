from __future__ import annotations

import asyncio

import fakeredis
import pytest

from voice_agent.jobs.queue import RedisJobQueue


@pytest.fixture
async def queue():
    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    q = RedisJobQueue(redis, namespace="t", worker_id="w1")
    yield q
    await q.close()


async def test_fifo_and_ack(queue):
    await queue.enqueue("a")
    await queue.enqueue("b")
    assert await queue.dequeue(0.1) == "a"
    assert await queue.dequeue(0.1) == "b"
    assert await queue.redis.lrange(queue.processing_key, 0, -1) == ["b", "a"]
    await queue.ack("a")
    await queue.ack("b")
    assert await queue.redis.llen(queue.processing_key) == 0
    assert await queue.dequeue(0.1) is None


async def test_delayed_jobs_become_ready(queue):
    await queue.enqueue("later", delay_seconds=0.2)
    assert await queue.dequeue(0.05) is None
    await asyncio.sleep(0.25)
    assert await queue.dequeue(0.1) == "later"


async def test_inflight_jobs_are_recovered_after_crash(queue):
    await queue.enqueue("x")
    assert await queue.dequeue(0.1) == "x"  # worker crashes before ack
    restarted = RedisJobQueue(queue.redis, namespace="t", worker_id="w1")
    assert await restarted.recover_inflight() == 1
    assert await restarted.dequeue(0.1) == "x"


async def test_ping(queue):
    assert await queue.ping()
