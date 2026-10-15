"""Sarah's outbound calls: request desk, bridge dialing, prank mode."""

from __future__ import annotations

import time
import uuid

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from voice_agent.agent_tools.outbound import OutboundCalls, OutboundRequest
from voice_agent.api.app import create_app
from voice_agent.domain.callers import CallerDirectory
from voice_agent.domain.people import PeopleDirectory
from voice_agent.telephony.el_bridge import OUTBOUND_CONTEXT, ElevenLabsBridge

EXTENSIONS = {"101": "mohamed", "102": "ahmed", "105": "sara"}


@pytest.fixture
def people() -> PeopleDirectory:
    return PeopleDirectory.load()


@pytest.fixture
def callers(people) -> CallerDirectory:
    return CallerDirectory(EXTENSIONS, {p.key for p in people})


@pytest.fixture
def desk(people, callers) -> OutboundCalls:
    return OutboundCalls(fakeredis.aioredis.FakeRedis(), people=people, callers=callers)


def test_extension_lookup(callers):
    assert callers.extension_for("ahmed") == "102"
    assert callers.extension_for("karim") is None


async def test_prank_request_is_queued_once_per_target(desk):
    first = await desk.request(target_key="ahmed", purpose="prank", caller_id="101")
    assert first == {"status": "calling", "name": "أحمد"}
    queued = await desk.next_request(wait_seconds=1)
    assert queued.target_key == "ahmed" and queued.extension == "102"
    assert queued.requested_by == "mohamed"
    again = await desk.request(target_key="ahmed", purpose="prank", caller_id="101")
    assert again["status"] == "too_soon"


async def test_requests_that_must_not_call_anyone(desk):
    assert (await desk.request(target_key="karim", purpose="prank", caller_id="101"))[
        "status"
    ] == "unknown_person"  # in the team, but no extension on file
    assert (await desk.request(target_key="mohamed", purpose="prank", caller_id="101"))[
        "status"
    ] == "same_person"
    assert (await desk.request(target_key="ahmed", purpose="sales", caller_id="101"))[
        "status"
    ] == "unsupported"
    assert await desk.next_request(wait_seconds=1) is None


async def test_requester_limit(desk):
    desk.per_requester_limit = 1
    assert (await desk.request(target_key="ahmed", purpose="prank", caller_id="101"))[
        "status"
    ] == "calling"
    assert (await desk.request(target_key="sara", purpose="prank", caller_id="101"))[
        "status"
    ] == "limit_reached"


async def test_stale_requests_are_dropped(desk):
    stale = OutboundRequest("ahmed", "102", "prank", "mohamed", time.time() - 300)
    await desk.redis.lpush(desk.queue_key, stale.to_json())
    assert await desk.next_request(wait_seconds=1) is None


class FakeAri:
    def __init__(self):
        self.calls = []

    async def originate(self, **kwargs):
        self.calls.append(kwargs)
        return {"id": "chan-1"}


async def test_bridge_dials_and_answers_in_prank_mode(people):
    ari = FakeAri()
    bridge = ElevenLabsBridge(
        api_key="k",
        agent_id="a",
        base_url="https://x",
        host="127.0.0.1",
        port=0,
        ari=ari,
        people=people,
        outbound=object(),
    )
    request = OutboundRequest("ahmed", "102", "prank", "mohamed", time.time())
    await bridge.place_call(request)
    call = ari.calls[0]
    assert call["endpoint"] == "PJSIP/102" and call["context"] == OUTBOUND_CONTEXT
    call_uuid = uuid.UUID(call["variables"]["VM_UUID"]).bytes
    assert bridge.limit_for(call_uuid) == 90

    variables = bridge.initiation(None, call_uuid)["dynamic_variables"]
    assert variables["call_mode"] == "prank"
    assert variables["caller_name"] == "أحمد" and variables["prank_by"] == "محمد"
    assert variables["opening"] == "ألو يا أحمد، عايزاك في حاجة مهمة."
    assert call_uuid not in bridge.pending_outbound  # used once

    normal = bridge.initiation("101", uuid.uuid4().bytes)["dynamic_variables"]
    assert normal["call_mode"] == "inbound"


async def test_failed_dial_forgets_the_pending_call(people):
    class BrokenAri:
        async def originate(self, **kwargs):
            raise RuntimeError("ARI down")

    bridge = ElevenLabsBridge(
        api_key="k",
        agent_id="a",
        base_url="https://x",
        host="127.0.0.1",
        port=0,
        ari=BrokenAri(),
        people=people,
        outbound=object(),
    )
    await bridge.place_call(OutboundRequest("ahmed", "102", "prank", None, time.time()))
    assert bridge.pending_outbound == {}


def test_call_person_endpoint_requires_the_secret_and_the_feature(container):
    container.settings.agent_tool_secret = "s" * 40
    with TestClient(create_app(container=container)) as client:
        body = {"conversation_id": "c1", "person_key": "ahmed", "purpose": "prank"}
        assert client.post("/agent/tools/call_person", json=body).status_code == 401
        headers = {"Authorization": "Bearer " + "s" * 40}
        reply = client.post("/agent/tools/call_person", json=body, headers=headers)
        assert reply.json() == {"status": "unavailable"}  # outbound not enabled
