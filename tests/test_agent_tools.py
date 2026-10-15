"""Grace-windowed task tools for the hosted voice agent (ElevenLabs)."""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from fastapi.testclient import TestClient

from voice_agent.agent_tools.window import TaskWindow
from voice_agent.api.app import create_app

SECRET = "s" * 40
GRACE = 0.15


@pytest.fixture
def window(container) -> TaskWindow:
    return TaskWindow(container, grace_seconds=GRACE, done_wait_seconds=1.0, poll_seconds=0.01)


@contextlib.asynccontextmanager
async def running_worker(worker):
    async def loop():
        while True:
            await worker.run_until_idle()
            await asyncio.sleep(0.01)

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_task_is_created_after_the_window_and_reported_done(window, worker, mock_monday):
    async with running_worker(worker):
        result = await window.submit(
            conversation_id="conv_1", assignee_key="ahmed", task="يجيب الكهربائي بكرة"
        )
    assert result["status"] == "created"
    assert result["assignee"] == "أحمد"
    assert [c["title"] for c in mock_monday.calls] == ["يجيب الكهربائي بكرة"]
    assert mock_monday.calls[0]["person_id"] == 222


async def test_nothing_is_saved_during_the_window(window, container):
    pending = asyncio.create_task(
        window.submit(conversation_id="conv_2", assignee_key="ahmed", task="يجيب الكهربائي")
    )
    await asyncio.sleep(GRACE / 3)
    assert container.store.status_counts() == {}
    await pending


async def test_correction_within_window_replaces_the_draft(window, worker, mock_monday):
    async with running_worker(worker):
        first = asyncio.create_task(
            window.submit(conversation_id="conv_3", assignee_key="ahmed", task="يجيب الكهربائي")
        )
        await asyncio.sleep(GRACE / 3)
        second = await window.submit(
            conversation_id="conv_3",
            assignee_key="mohamed",
            task="يجيب الكهربائي",
            mode="replace_last",
        )
        assert (await first)["status"] == "replaced"
    assert second["status"] == "created"
    assert [c["person_id"] for c in mock_monday.calls] == [111]


async def test_extra_request_within_window_adds_a_task(window, worker, mock_monday):
    async with running_worker(worker):
        first = asyncio.create_task(
            window.submit(conversation_id="conv_4", assignee_key="ahmed", task="يجيب الكهربائي")
        )
        await asyncio.sleep(GRACE / 3)
        second = await window.submit(
            conversation_id="conv_4", assignee_key="michael", task="يشتري كاميرا"
        )
        assert (await first)["status"] == "created"
    assert second["status"] == "created"
    assert sorted(c["title"] for c in mock_monday.calls) == ["يجيب الكهربائي", "يشتري كاميرا"]


async def test_cancel_within_window_saves_nothing(window, container):
    pending = asyncio.create_task(
        window.submit(conversation_id="conv_5", assignee_key="ahmed", task="يجيب الكهربائي")
    )
    await asyncio.sleep(GRACE / 3)
    assert await window.cancel_last(conversation_id="conv_5") == {"status": "cancelled"}
    assert (await pending)["status"] == "cancelled"
    await asyncio.sleep(GRACE)
    assert container.store.status_counts() == {}
    assert await window.cancel_last(conversation_id="conv_5") == {"status": "nothing_pending"}


async def test_retried_webhook_joins_the_same_draft(window, worker, mock_monday):
    async with running_worker(worker):
        results = await asyncio.gather(
            window.submit(conversation_id="conv_6", assignee_key="ahmed", task="يجيب الكهربائي"),
            window.submit(conversation_id="conv_6", assignee_key="ahmed", task="يجيب  الكهربائي"),
        )
    assert [r["status"] for r in results] == ["created", "created"]
    assert results[0]["job_id"] == results[1]["job_id"]
    assert len(mock_monday.calls) == 1


async def test_unknown_or_unmapped_assignee_is_rejected_before_waiting(window, container):
    unknown = await window.submit(conversation_id="conv_7", assignee_key="shady", task="مهمة")
    assert unknown["status"] == "invalid" and unknown["problem"].startswith("unknown_person")
    unmapped = await window.submit(conversation_id="conv_7", assignee_key="karim", task="مهمة")
    assert unmapped["status"] == "invalid"  # in the directory, but no Monday person id
    assert container.store.status_counts() == {}


async def test_slow_worker_reports_queued_not_done(container):
    window = TaskWindow(container, grace_seconds=0.01, done_wait_seconds=0.05, poll_seconds=0.01)
    result = await window.submit(conversation_id="conv_8", assignee_key="ahmed", task="مهمة")
    assert result["status"] == "queued"
    assert container.store.status_counts() == {"queued": 1}


async def test_failed_job_is_reported_as_failed(container, worker, mock_monday):
    mock_monday.fail_times = 99  # every attempt fails until dead_letter
    window = TaskWindow(container, grace_seconds=0.01, done_wait_seconds=3.0, poll_seconds=0.01)
    async with running_worker(worker):
        result = await window.submit(conversation_id="conv_9", assignee_key="ahmed", task="مهمة")
    assert result["status"] == "failed"


async def test_requester_is_mapped_from_caller_id(window, worker, container):
    container.callers._mappings["101"] = "mohamed"
    async with running_worker(worker):
        result = await window.submit(
            conversation_id="conv_10", assignee_key="ahmed", task="مهمة", caller_id="101"
        )
    job = container.store.get(result["job_id"])
    assert job.payload["requester_key"] == "mohamed"
    assert job.intent["requester"] == {"person_key": "mohamed", "caller_number": "101"}
    assert job.intent["source"] == "elevenlabs_agent"


# ------------------------------------------------------------------ HTTP endpoints


@pytest.fixture
def client(container):
    container.settings.agent_tool_secret = SECRET
    container.settings.agent_grace_seconds = 0.01
    container.settings.agent_done_wait_seconds = 0.05
    with TestClient(create_app(container=container)) as c:
        yield c


def test_tools_require_the_bearer_secret(client):
    body = {"conversation_id": "conv_h1", "assignee_key": "ahmed", "task": "مهمة"}
    assert client.post("/agent/tools/submit_task", json=body).status_code == 401
    wrong = {"Authorization": "Bearer nope"}
    assert client.post("/agent/tools/submit_task", json=body, headers=wrong).status_code == 401
    ok = client.post(
        "/agent/tools/submit_task", json=body, headers={"Authorization": f"Bearer {SECRET}"}
    )
    assert ok.status_code == 200
    assert ok.json()["status"] in {"queued", "created"}


def test_tools_validate_input(client):
    headers = {"Authorization": f"Bearer {SECRET}"}
    bad = {"conversation_id": "has spaces", "assignee_key": "ahmed", "task": "مهمة"}
    assert client.post("/agent/tools/submit_task", json=bad, headers=headers).status_code == 422
    long = {"conversation_id": "c1", "assignee_key": "ahmed", "task": "x" * 256}
    assert client.post("/agent/tools/submit_task", json=long, headers=headers).status_code == 422
    mode = {"conversation_id": "c1", "assignee_key": "ahmed", "task": "t", "mode": "delete_all"}
    assert client.post("/agent/tools/submit_task", json=mode, headers=headers).status_code == 422


def test_tools_do_not_exist_without_a_secret(container):
    container.settings.agent_tool_secret = ""
    with TestClient(create_app(container=container)) as c:
        body = {"conversation_id": "c1", "assignee_key": "ahmed", "task": "مهمة"}
        headers = {"Authorization": "Bearer anything"}
        assert c.post("/agent/tools/submit_task", json=body, headers=headers).status_code == 404


def test_only_agent_tools_are_reachable_through_the_tunnel(client):
    via_tunnel = {"CF-Connecting-IP": "203.0.113.9"}
    assert client.get("/health", headers=via_tunnel).status_code == 404
    assert client.post("/simulate", json={"text": "x"}, headers=via_tunnel).status_code == 404
    assert client.get("/jobs/abc", headers=via_tunnel).status_code == 404
    assert client.get("/health").status_code == 200  # local access unchanged
    body = {"conversation_id": "conv_t", "assignee_key": "ahmed", "task": "مهمة"}
    headers = {**via_tunnel, "Authorization": f"Bearer {SECRET}"}
    assert client.post("/agent/tools/submit_task", json=body, headers=headers).status_code == 200
