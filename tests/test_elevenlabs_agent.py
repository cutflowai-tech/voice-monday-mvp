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
    assert props["caller_id"]["dynamic_variable"] == "caller_id"  # set by the AudioSocket bridge
    assert props["assignee_key"]["enum"] == ["mohamed", "ahmed"]  # only people with Monday ids
    assert submit["pre_tool_speech"] == "force"
    assert submit["tool_call_sound"] == "typing"  # ElevenLabs keyboard while saving
    assert 5 <= submit["response_timeout_secs"] <= 300

    agent_body = by_path[("POST", "/v1/convai/agents/create")]
    assert agent_body["platform_settings"]["auth"]["enable_auth"] is True  # signed URLs only
    agent = agent_body["conversation_config"]
    assert agent["agent"]["first_message"] == "{{opening}}"
    placeholders = agent["agent"]["dynamic_variables"]["dynamic_variable_placeholders"]
    assert set(placeholders) == {
        "opening",
        "caller_name",
        "caller_id",
        "caller_gender",
        "call_mode",
        "prank_by",
        "ack_hint",
        "done_hint",
    }
    assert agent["agent"]["prompt"]["built_in_tools"]["end_call"]["params"] == {
        "system_tool_type": "end_call"
    }
    prompt_text = agent["agent"]["prompt"]["prompt"]
    assert "{{call_mode}}" in prompt_text and "خدتك عليه" in prompt_text
    assert "{{caller_gender}}" in prompt_text and "أبغى" in prompt_text  # Gulf words banned
    # default: Egyptian voice on the fast model -> no audio tags anywhere
    assert agent["tts"]["model_id"] == "eleven_v4_turbo"
    assert "expressive_mode" not in agent["tts"] and "[" not in prompt_text
    assert {"محمد", "أحمد", "حمادة"} <= set(agent["asr"]["keywords"])
    assert "{{ack_hint}}" in agent["agent"]["prompt"]["prompt"]
    assert agent["turn"]["soft_timeout_config"] == {"timeout_seconds": -1}  # no fillers
    assert agent["agent"]["language"] == "ar"
    assert agent["agent"]["prompt"]["tool_ids"] == ["tool_2", "tool_3"]
    assert "mohamed: محمد (male" in agent["agent"]["prompt"]["prompt"]
    assert "Sarah (سارة)" in agent["agent"]["prompt"]["prompt"]
    prompt = agent["agent"]["prompt"]["prompt"]
    assert "karim" not in prompt
    assert '"already_saved"' in prompt
    assert "Never say a task is saved, cancelled or changed unless the tool result" in prompt
    assert "{{caller_name}}" in prompt and "{people}" not in prompt
    assert agent["tts"]["voice_id"] == "L10lEremDiJfPicq5CPh"  # Yasmine (Egyptian)
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


def test_prompt_matches_the_voice_model():
    from voice_agent.sarah import build_prompt

    people = list(PeopleDirectory.load())[:2]
    expressive = build_prompt(people, expressive=True)
    plain = build_prompt(people, expressive=False)
    assert "[laughs]" in expressive and "[happy]" in expressive
    assert "[" not in plain  # no tag examples at all on a non-expressive model
    assert "Never write anything in square brackets" in plain


def test_expressive_model_gets_audio_tags(el_settings):
    from voice_agent.elevenlabs_agent import build_tts

    el_settings.elevenlabs_agent_tts_model = "eleven_v3_conversational"
    tts = build_tts(el_settings)
    assert tts["expressive_mode"] is True
    assert {t["tag"] for t in tts["suggested_audio_tags"]} >= {"[laughs]", "[sighs]"}


def test_call_person_tool_only_when_outbound_is_on(el_settings):
    from voice_agent.elevenlabs_agent import build_tools

    people = list(PeopleDirectory.load())
    assert "call_person" not in build_tools(el_settings, people[:2], "sec")
    tools = build_tools(el_settings, people[:2], "sec", callable_people=people[:3])
    schema = tools["call_person"]["api_schema"]
    assert schema["url"].endswith("/agent/tools/call_person")
    assert schema["request_body_schema"]["properties"]["person_key"]["enum"] == [
        p.key for p in people[:3]
    ]
    assert schema["request_body_schema"]["properties"]["purpose"]["enum"] == ["prank"]
    reveal = tools["prank_reveal"]
    assert reveal["type"] == "client" and reveal["expects_response"] is True
    assert reveal["pre_tool_speech"] == "off"


def test_prank_prompt_never_names_the_requester():
    from voice_agent.sarah import PUNCHLINES, build_prompt

    prompt = build_prompt(list(PeopleDirectory.load())[:1], expressive=False)
    assert "prank_reveal" in prompt and "Never say who asked for the prank" in prompt
    assert "مقلب من" not in prompt
    assert all("خَدتَك" in t for t in PUNCHLINES["m"])  # fatha: said to a man
    assert all("خَدتِك" in t for t in PUNCHLINES["f"])


async def test_sync_renders_punchline_clips_once(el_settings, tmp_path):
    el_settings.agent_outbound_enabled = True
    posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return httpx.Response(200, content=b"\x00\x00" * 800)

    async with httpx.AsyncClient(
        base_url="https://api.elevenlabs.io", transport=httpx.MockTransport(handler)
    ) as client:
        sync = ElevenLabsAgentSync(el_settings, client, tmp_path / "state.json")
        first = await sync.render_punchlines(tmp_path / "prank")
        second = await sync.render_punchlines(tmp_path / "prank")
    assert len(first) == 3 and second == []  # one clip for men, two for women
    assert all(p.startswith("/v1/text-to-speech/") for p in posts)
