"""Job store state machine, worker retries, dead letter, idempotency, recovery."""

from __future__ import annotations

import pytest

from voice_agent.connectors.base import (
    ConnectorRegistry,
    PermanentConnectorError,
)
from voice_agent.conversation.engine import ConversationState
from voice_agent.db import Database
from voice_agent.jobs.models import JobSpec, JobStatus
from voice_agent.jobs.queue import InMemoryJobQueue
from voice_agent.jobs.service import make_idempotency_key
from voice_agent.jobs.store import JobStore
from voice_agent.jobs.worker import RetryPolicy, Worker

from .conftest import EXAMPLE

SPEC = JobSpec(
    connector="monday",
    operation="create_task",
    payload={"title": "شراء هارد للسيرفر", "assignee": "Mohamed", "assignee_key": "mohamed"},
)


class FakeClock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(tmp_path, clock) -> JobStore:
    db = Database(tmp_path / "jobs.db")
    db.migrate()
    return JobStore(db, clock=clock)


def create(store: JobStore, key: str = "key-1", max_attempts: int = 4):
    return store.create(
        SPEC, idempotency_key=key, transcript=EXAMPLE, intent={"action": "create_task"},
        max_attempts=max_attempts,
    )  # fmt: skip


def test_migrations_are_idempotent(tmp_path):
    db = Database(tmp_path / "x.db")
    assert db.migrate() == 1
    assert db.migrate() == 1


def test_status_transitions_and_events(store):
    job, created = create(store)
    assert created and job.status is JobStatus.QUEUED

    claimed = store.claim(job.id, lease_seconds=60)
    assert claimed.status is JobStatus.PROCESSING and claimed.attempts == 1
    assert store.claim(job.id, lease_seconds=60) is None  # cannot be claimed twice

    done = store.complete(claimed, result={"item_id": "42"}, external_ref="42")
    assert done.status is JobStatus.COMPLETED and done.external_ref == "42"
    assert store.claim(job.id, lease_seconds=60) is None  # completed jobs never rerun

    statuses = [e.status for e in store.events(job.id)]
    assert statuses == [JobStatus.QUEUED, JobStatus.PROCESSING, JobStatus.COMPLETED]


def test_retry_then_dead_letter(store, clock):
    job, _ = create(store, max_attempts=2)
    j = store.claim(job.id, 60)
    j = store.schedule_retry(j, error="503", delay_seconds=10, uncertain=False)
    assert j.status is JobStatus.QUEUED
    assert store.claim(job.id, 60) is None  # not due yet
    clock.now += 11
    j = store.claim(job.id, 60)
    assert j.attempts == 2
    j = store.dead_letter(j, error="503 again")
    assert j.status is JobStatus.DEAD_LETTER
    assert j.last_error == "503 again"
    assert j.transcript == EXAMPLE  # original request preserved


def test_duplicate_key_returns_original(store):
    first, created1 = create(store, key="same")
    second, created2 = create(store, key="same")
    assert created1 and not created2
    assert first.id == second.id


def test_expired_lease_is_reclaimed_and_marked_uncertain(store, clock):
    job, _ = create(store)
    first = store.claim(job.id, lease_seconds=30)
    assert not first.prior_attempt_uncertain
    clock.now += 31  # worker crashed mid-request
    assert store.find_recoverable(stale_after_seconds=5) == [job.id]
    second = store.claim(job.id, lease_seconds=30)
    assert second.attempts == 2
    assert second.prior_attempt_uncertain
    # The crashed worker's late completion is rejected (it lost the lease).
    assert store.complete(first, result={}, external_ref="x") is None


def test_idempotency_key_is_stable_per_turn():
    a = make_idempotency_key("call-1", 3, "حطي تاسك")
    assert a == make_idempotency_key("call-1", 3, "حطي تاسك")
    assert a != make_idempotency_key("call-1", 4, "حطي تاسك")
    assert a != make_idempotency_key("call-2", 3, "حطي تاسك")


# ------------------------------------------------------------------ worker
async def test_worker_completes_job(container, worker, mock_monday):
    reply = await container.engine.handle(ConversationState(), EXAMPLE)
    await worker.run_until_idle()
    job = container.store.get(reply.job.id)
    assert job.status is JobStatus.COMPLETED
    assert job.external_ref == mock_monday.items[job.idempotency_key]["id"]
    assert job.result["item_name"] == "شراء هارد للسيرفر"
    assert mock_monday.calls[0]["person_id"] == 111


async def test_transient_failures_retry_with_backoff(container, worker, mock_monday):
    mock_monday.fail_times = 2
    reply = await container.engine.handle(ConversationState(), EXAMPLE)
    await worker.run_until_idle()
    job = container.store.get(reply.job.id)
    assert job.status is JobStatus.COMPLETED
    assert job.attempts == 3
    assert len(mock_monday.items) == 1  # retries did not create duplicates
    messages = [e.message for e in container.store.events(job.id)]
    assert sum("retry scheduled" in m for m in messages) == 2


async def test_dead_letter_after_retries_exhausted(container, worker, mock_monday):
    mock_monday.fail_times = 100
    reply = await container.engine.handle(ConversationState(), EXAMPLE)
    await worker.run_until_idle()
    job = container.store.get(reply.job.id)
    assert job.status is JobStatus.DEAD_LETTER
    assert job.attempts == job.max_attempts == 4  # 1 attempt + 3 retries
    assert "simulated Monday outage" in job.last_error
    assert job.transcript == EXAMPLE and job.intent["assignee"] == "Mohamed"


async def test_permanent_failure_is_not_retried(container, worker, mock_monday, monkeypatch):
    async def bad(**kwargs):
        raise PermanentConnectorError("InvalidColumnIdException: bad column")

    monkeypatch.setattr(mock_monday, "create_task", bad)
    reply = await container.engine.handle(ConversationState(), EXAMPLE)
    await worker.run_until_idle()
    job = container.store.get(reply.job.id)
    assert job.status is JobStatus.FAILED
    assert job.attempts == 1


async def test_jobs_run_after_simulated_hangup(container, worker, mock_monday):
    """The call session ends; the worker still executes every accepted job."""
    state = ConversationState(call_id="call-123")
    await container.engine.handle(state, EXAMPLE)
    await container.engine.handle(state, "حطي تاسك لأحمد إنه يراجع الميزانية")
    state.ended = True  # caller hangs up; session state is discarded
    del state

    await worker.run_until_idle()
    counts = container.store.status_counts()
    assert counts == {"completed": 2}
    assert len(mock_monday.items) == 2


async def test_duplicate_queue_messages_do_not_duplicate_items(container, worker, mock_monday):
    reply = await container.engine.handle(ConversationState(), EXAMPLE)
    for _ in range(3):  # e.g. reconciler + redelivery
        await container.queue.enqueue(reply.job.id)
    await worker.run_until_idle()
    assert len(mock_monday.calls) == 1


async def test_reconciler_recovers_job_lost_from_queue(tmp_path, mock_monday):
    db = Database(tmp_path / "r.db")
    db.migrate()
    clock = FakeClock()
    store = JobStore(db, clock=clock)
    job, _ = create(store)  # persisted, but never reached the queue (Redis down)
    queue = InMemoryJobQueue()
    registry = ConnectorRegistry()
    registry.register(mock_monday)
    worker = Worker(store=store, queue=queue, connectors=registry, retry=RetryPolicy(0.01, 0.02))
    clock.now += 60
    assert await worker.reconcile() == 1
    await worker.run_until_idle()
    assert store.get(job.id).status is JobStatus.COMPLETED


def test_retry_policy_is_exponential_and_honours_retry_after():
    policy = RetryPolicy(base_seconds=2, max_seconds=100, jitter=0)
    assert [policy.delay(n) for n in (1, 2, 3, 4)] == [2, 4, 8, 16]
    assert policy.delay(1, retry_after=30) == 30
    assert policy.delay(10) == 100
