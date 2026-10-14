from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from voice_agent.api.app import create_app

from .conftest import EXAMPLE


@pytest.fixture
def client(container):
    with TestClient(create_app(container=container)) as c:
        yield c


def test_health_and_ready(client):
    assert client.get("/health").json() == {"status": "ok"}
    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json()["checks"] == {"database": True, "queue": True, "connector:monday": True}


def test_simulate_returns_job_intent_and_ack(client):
    response = client.post("/simulate", json={"text": EXAMPLE})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert body["job_id"]
    assert body["intent"]["action"] == "create_task"
    assert body["intent"]["assignee"] == "Mohamed"
    assert body["intent"]["task"] == "شراء هارد للسيرفر"
    assert "محمد" in body["acknowledgment"]
    assert body["call_active"] is True


def test_job_status_and_events(client, container, worker):
    job_id = client.post("/simulate", json={"text": EXAMPLE}).json()["job_id"]
    assert client.get(f"/jobs/{job_id}").json()["status"] == "queued"

    client.portal.call(worker.run_until_idle)  # run the worker on the app's event loop

    job = client.get(f"/jobs/{job_id}").json()
    assert job["status"] == "completed"
    assert job["external_ref"]
    assert job["transcript"] == EXAMPLE
    events = client.get(f"/jobs/{job_id}/events").json()["events"]
    assert [e["status"] for e in events] == ["queued", "processing", "completed"]


def test_unknown_job_is_404(client):
    assert client.get("/jobs/nope").status_code == 404
    assert client.get("/jobs/nope/events").status_code == 404


def test_input_validation(client):
    assert client.post("/simulate", json={"text": ""}).status_code == 422
    assert client.post("/simulate", json={"text": "x" * 1001}).status_code == 422
    assert client.post("/simulate", json={}).status_code == 422
    bad_key = {"text": EXAMPLE, "idempotency_key": "bad key with spaces"}
    assert client.post("/simulate", json=bad_key).status_code == 422


def test_idempotent_resubmission(client, container, worker, mock_monday):
    headers = {"Idempotency-Key": "retry-key-123"}
    first = client.post("/simulate", json={"text": EXAMPLE}, headers=headers).json()
    second = client.post("/simulate", json={"text": EXAMPLE}, headers=headers).json()
    assert first["job_id"] == second["job_id"]
    assert second["duplicate"] is True
    client.portal.call(worker.run_until_idle)
    assert len(mock_monday.items) == 1


def test_multi_turn_session_via_api(client):
    first = client.post("/simulate", json={"text": "حطي تاسك لمحمد"}).json()
    assert first["reply_kind"] == "clarification" and first["job_id"] is None
    second = client.post(
        "/simulate", json={"text": "يشتري هارد للسيرفر", "session_id": first["session_id"]}
    ).json()
    assert second["reply_kind"] == "accepted"
    assert second["intent"]["task"] == "شراء هارد للسيرفر"


def test_wait_for_final_status(client, container, worker):
    import threading

    stop = threading.Event()

    def run_worker():
        while not stop.is_set():
            client.portal.call(worker.run_until_idle, 0.2)

    thread = threading.Thread(target=run_worker)
    thread.start()
    try:
        body = client.post("/simulate", json={"text": EXAMPLE, "wait_seconds": 5}).json()
    finally:
        stop.set()
        thread.join()
    assert body["status"] == "completed"
    assert body["job"]["result"]["item_name"] == "شراء هارد للسيرفر"
