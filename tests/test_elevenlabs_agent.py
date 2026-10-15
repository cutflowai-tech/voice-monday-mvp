"""ElevenLabs agent sync builds documented payloads and never duplicates resources."""

from __future__ import annotations

import json

import httpx
import pytest

from voice_agent.config import Settings
from voice_agent.domain.people import PeopleDirectory
from voice_agent.elevenlabs_agent import ElevenLabsAgentSync, missing_settings


@pytest.fixture
def el_settings(monkeypatch) -> Settings:
    monkeypatch.setenv("MONDAY_MOHAMED_PERSON_ID", "2")
    monkeypatch.setenv("MONDAY_AHMED_PERSON_ID", "6")
    return Settings(
        elevenlabs_api_key="xi-test",
        agent_tool_secret="t" * 40,
        agent_public_url="https://tools.example.com/",
        elevenlabs_sip_username="pbx",
        elevenlabs_sip_password="p" * 24,
    )


class FakeElevenLabs:
    def __init__(self):
        self.requests: list[tuple[str, str, dict]] = []
        self.counter = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        assert request.headers["xi-api-key"] == "xi-test"
        self.requests.append((request.method, request.url.path, body))
        self.counter += 1
        path = request.url.path
        if path == "/v1/convai/secrets":
            return httpx.Response(200, json={"type": "stored", "secret_id": "sec_1", "name": "x"})
        if path == "/v1/convai/tools" and request.method == "POST":
            return httpx.Response(200, json={"id": f"tool_{self.counter}", "tool_config": {}})
        if path == "/v1/convai/agents/create":
            return httpx.Response(200, json={"agent_id": "agent_1"})
        if path == "/v1/convai/phone-numbers":
            return httpx.Response(200, json={"phone_number_id": "phone_1"})
        return httpx.Response(200, json={})


async def sync_once(el_settings, fake, state_path):
    async with httpx.AsyncClient(
        base_url="https://api.elevenlabs.io", transport=httpx.MockTransport(fake.handler)
    ) as client:
        return await ElevenLabsAgentSync(el_settings, client, state_path).run(
            PeopleDirectory.load()
        )


async def test_first_sync_creates_everything_with_documented_shapes(el_settings, tmp_path):
    fake = FakeElevenLabs()
    state = await sync_once(el_settings, fake, tmp_path / "state.json")
    assert state == {
        "secret_id": "sec_1",
        "tool_ids": {"submit_task": "tool_2", "cancel_last": "tool_3"},
        "agent_id": "agent_1",
        "phone_number_id": "phone_1",
    }
    by_path = {(m, p): b for m, p, b in fake.requests}

    secret = by_path[("POST", "/v1/convai/secrets")]
    assert secret["value"] == f"Bearer {'t' * 40}"

    submit = fake.requests[1][2]["tool_config"]
    assert submit["type"] == "webhook"
    assert submit["api_schema"]["url"] == "https://tools.example.com/agent/tools/submit_task"
    assert submit["api_schema"]["request_headers"] == {"Authorization": {"secret_id": "sec_1"}}
    props = submit["api_schema"]["request_body_schema"]["properties"]
    assert props["conversation_id"]["dynamic_variable"] == "system__conversation_id"
    assert props["caller_id"]["dynamic_variable"] == "system__caller_id"
    assert props["assignee_key"]["enum"] == ["mohamed", "ahmed"]  # only people with Monday ids
    assert submit["pre_tool_speech"] == "force"
    assert 5 <= submit["response_timeout_secs"] <= 300

    agent = by_path[("POST", "/v1/convai/agents/create")]["conversation_config"]
    assert agent["agent"]["first_message"] == "ألو"
    assert agent["agent"]["language"] == "ar"
    assert agent["agent"]["prompt"]["tool_ids"] == ["tool_2", "tool_3"]
    assert "mohamed: محمد" in agent["agent"]["prompt"]["prompt"]
    assert "karim" not in agent["agent"]["prompt"]["prompt"]
    assert agent["tts"]["voice_id"] == "I3u6waC588j43py1kDN2"
    assert agent["turn"]["turn_eagerness"] == "eager"

    phone = by_path[("POST", "/v1/convai/phone-numbers")]
    assert phone["provider"] == "sip_trunk" and phone["phone_number"] == "778"
    assert phone["agent_id"] == "agent_1"
    assert phone["inbound_trunk_config"]["credentials"]["username"] == "pbx"

    saved = (tmp_path / "state.json").read_text()
    assert "t" * 40 not in saved and "p" * 24 not in saved  # ids only, never secrets


async def test_second_sync_updates_in_place(el_settings, tmp_path):
    state_path = tmp_path / "state.json"
    await sync_once(el_settings, FakeElevenLabs(), state_path)
    fake = FakeElevenLabs()
    await sync_once(el_settings, fake, state_path)
    assert [(m, p) for m, p, _ in fake.requests] == [
        ("PATCH", "/v1/convai/tools/tool_2"),
        ("PATCH", "/v1/convai/tools/tool_3"),
        ("PATCH", "/v1/convai/agents/agent_1"),
    ]


def test_missing_settings_are_listed():
    problems = missing_settings(Settings(agent_public_url="http://insecure"))
    assert "AGENT_TOOL_SECRET is required" in problems
    assert "AGENT_PUBLIC_URL must start with https://" in problems
