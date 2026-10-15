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


class Socket:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        import json

        self.sent.append(json.loads(data))


async def test_prank_reveal_plays_the_clip_mutes_and_ends_the_call():
    import asyncio

    from voice_agent.telephony.el_bridge import BridgeCall

    from .test_el_bridge import FakeWriter

    call = BridgeCall(asyncio.StreamReader(), FakeWriter(), open_agent=None)
    call.punchline = bytes(640)  # 20 ms at 16 kHz
    ws = Socket()
    pacer = asyncio.create_task(call._pace_playback())
    call.connecting = False
    ended = await call._client_tool(
        ws, {"tool_name": "prank_reveal", "tool_call_id": "t1", "expects_response": True}
    )
    pacer.cancel()
    assert ended and call.muted and call.punchline_started
    assert ws.sent == [
        {"type": "client_tool_result", "tool_call_id": "t1", "result": "played", "is_error": False}
    ]


async def test_prank_reveal_without_a_clip_lets_her_say_it():
    import asyncio

    from voice_agent.telephony.el_bridge import BridgeCall

    from .test_el_bridge import FakeWriter

    call = BridgeCall(asyncio.StreamReader(), FakeWriter(), open_agent=None)
    ws = Socket()
    ended = await call._client_tool(
        ws, {"tool_name": "prank_reveal", "tool_call_id": "t2", "expects_response": True}
    )
    assert not ended and not call.muted
    assert ws.sent[0]["result"] == "unavailable"
    await call._client_tool(ws, {"tool_name": "x", "tool_call_id": "t3", "expects_response": True})
    assert ws.sent[1]["is_error"] is True


async def test_prank_call_gets_a_clip_for_the_targets_gender(people, tmp_path):
    (tmp_path / "VOICE-m-0.pcm").write_bytes(b"m" * 64)
    (tmp_path / "VOICE-f-0.pcm").write_bytes(b"f" * 64)
    bridge = ElevenLabsBridge(
        api_key="k",
        agent_id="a",
        base_url="https://x",
        host="127.0.0.1",
        port=0,
        ari=FakeAri(),
        people=people,
        outbound=object(),
        punchline_dir=str(tmp_path),
        voice_id="VOICE",
    )
    await bridge.place_call(OutboundRequest("ahmed", "102", "prank", "mohamed", time.time()))
    call_uuid = uuid.UUID(bridge.ari.calls[0]["variables"]["VM_UUID"]).bytes
    bridge.initiation(None, call_uuid)
    assert bridge.punchline_for(call_uuid) == b"m" * 64
    assert bridge.punchline_for(call_uuid) is None  # used once


async def test_bridge_plays_the_punchline_itself_after_any_short_reply():
    import asyncio

    from voice_agent.telephony.el_bridge import BridgeCall

    from .test_el_bridge import FakeWriter

    call = BridgeCall(asyncio.StreamReader(), FakeWriter(), open_agent=None)
    call.punchline = bytes(640)
    call.connecting = False
    now = time.monotonic()
    call._agent_spoke_at = now - 3  # her opening is over
    call._count_caller_speech(0.3)  # «إيه»
    call._last_caller_speech = now - 0.6  # ... then a pause
    await asyncio.wait_for(call._auto_punchline(), 2)
    assert call.punchline_started and call.muted and call._finished.is_set()


async def test_hello_on_pickup_does_not_trigger_the_punchline():
    import asyncio

    from voice_agent.telephony.el_bridge import BridgeCall

    from .test_el_bridge import FakeWriter

    call = BridgeCall(asyncio.StreamReader(), FakeWriter(), open_agent=None)
    call.punchline = bytes(640)
    call._count_caller_speech(0.5)  # «ألو؟» before she has said anything
    call._agent_spoke_at = time.monotonic()
    call._count_caller_speech(0.5)  # still within her first 1.5 s
    assert call._caller_speech_s == 0


async def test_auto_watcher_does_not_cut_a_punchline_started_by_the_tool():
    import asyncio

    from voice_agent.telephony.el_bridge import BridgeCall

    from .test_el_bridge import FakeWriter

    call = BridgeCall(asyncio.StreamReader(), FakeWriter(), open_agent=None)
    call.punchline = bytes(16000)  # 0.5 s at 16 kHz
    watcher = asyncio.create_task(call._auto_punchline())
    tool = asyncio.create_task(
        call._client_tool(Socket(), {"tool_name": "prank_reveal", "tool_call_id": "t"})
    )
    await asyncio.sleep(0.2)
    assert not watcher.done()  # still waiting for the clip, not ending the call
    assert await tool is True
    await asyncio.wait_for(watcher, 1)
