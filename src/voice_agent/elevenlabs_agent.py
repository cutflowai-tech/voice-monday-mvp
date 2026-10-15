"""ElevenLabs Agent configured from code (``voice-agent el-agent sync``).

API facts used here (checked against elevenlabs.io/docs/api-reference, Oct 2026):

* ``POST /v1/convai/secrets`` {type:"new", name, value} -> {secret_id}
* ``POST /v1/convai/tools`` / ``PATCH /v1/convai/tools/{tool_id}`` with
  ``{"tool_config": {"type": "webhook", "api_schema": {...}, ...}}``. A body property
  with ``"dynamic_variable"`` is filled by the platform (e.g. ``system__conversation_id``).
  Header values may be a string or a secret locator ``{"secret_id": ...}``.
  ``pre_tool_speech``: auto|force|off; ``execution_mode``: immediate|post_tool_speech|async;
  ``interruption_mode``: allow|disable_during_tool|disable_during_tool_and_turn;
  ``response_timeout_secs``: 5..300.
* ``POST /v1/convai/agents/create`` / ``PATCH /v1/convai/agents/{agent_id}`` with
  ``conversation_config`` {agent{first_message, language, prompt{prompt, llm, temperature,
  tool_ids}}, tts{model_id, voice_id}, turn{turn_timeout, turn_eagerness, speculative_turn},
  asr{provider, quality}}.
* ``POST /v1/convai/phone-numbers`` {phone_number, label, provider:"sip_trunk", agent_id,
  inbound_trunk_config{media_encryption, credentials{username, password}}}.
  The PBX dials ``sip:<phone_number>@sip.rtc.elevenlabs.io``; header ``X-CALLER-ID``
  becomes ``system__caller_id``.

IDs created here are kept in ``data/elevenlabs-state.json`` so re-running ``sync``
updates instead of duplicating. No secret values are written there.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from voice_agent.config import Settings
from voice_agent.domain.people import PeopleDirectory, Person

SECRET_NAME = "voice-monday-tool-bearer"  # noqa: S105 (a name, not a value)
TOOL_SUBMIT = "submit_task"
TOOL_CANCEL = "cancel_last"

PROMPT = """\
You are the female voice assistant on the office phone line. You speak ONLY Egyptian
Arabic (Cairene), warm and very brief, always in female forms (هقول، حاضر، سجلت).
Your only job is to put tasks for team members on Monday.

People you can assign tasks to (use exactly these keys):
{people}

For every task:
1. Reply with ONE short sentence that repeats the request, for example
   «تمام، هقول لأحمد يجيب الكهربائي بكرة», then call submit_task with mode "new".
2. When submit_task returns:
   - "created": say «Done، خلاص حطيت التاسك» (you may vary it slightly), then «حاجة تانية؟»
   - "queued": «بسجّلها دلوقتي، هتظهر على Monday كمان شوية.»
   - "replaced": say nothing about that earlier task.
   - "cancelled": «تمام، لغيتها.»
   - "invalid" with unknown_person: ask «التاسك دي لمين؟». Other "invalid": ask them to
     say the task again briefly.
   - "failed": «للأسف حصلت مشكلة ومقدرتش أسجلها، ممكن تقولها تاني؟»
3. If, right after your confirmation, the caller corrects you (another person, other
   details, «لا قصدي ...»), call submit_task with mode "replace_last" and the full
   corrected task. If they add another task, call submit_task with mode "new". If they
   cancel («لا الغيها»، «بلاش»), call cancel_last.

Rules:
- Never say a task is saved before submit_task returns "created".
- Never guess or invent a person. If the name is unclear or not in the list, ask who.
- The task text is the caller's own words in Egyptian Arabic, without the person's
  name, keeping time words such as بكرة، بعد بكرة، يوم الخميس.
- Do not ask for confirmation, and do not make small talk.
- For anything other than tasks say «أنا بسجل تاسكات بس.»
- When the caller says goodbye, answer «مع السلامة».
"""


def assignable_people(settings: Settings, directory: PeopleDirectory) -> list[Person]:
    """Only people with a Monday id can receive tasks, so only they are offered."""
    ids = settings.monday_person_ids()
    return [p for p in directory if p.key in ids]


def build_prompt(people: list[Person]) -> str:
    lines = []
    for p in people:
        aliases = [a for a in p.aliases if any("؀" <= ch <= "ۿ" for ch in a)]
        also = f" (also: {'، '.join(aliases)})" if aliases else ""
        lines.append(f"- {p.key}: {p.name_ar}{also}")
    return PROMPT.format(people="\n".join(lines))


def build_tools(settings: Settings, people: list[Person], secret_id: str) -> dict[str, dict]:
    base = settings.agent_public_url.rstrip("/")
    headers = {"Authorization": {"secret_id": secret_id}}
    conversation = {"type": "string", "dynamic_variable": "system__conversation_id"}
    submit = {
        "type": "webhook",
        "name": TOOL_SUBMIT,
        "description": (
            "Save a task for a team member on Monday. Call it right after you repeat the "
            "task to the caller. It waits a moment for corrections, then returns the result."
        ),
        "response_timeout_secs": 15,
        "pre_tool_speech": "force",
        "execution_mode": settings.elevenlabs_tool_execution_mode,
        "interruption_mode": "allow",
        "api_schema": {
            "url": f"{base}/agent/tools/{TOOL_SUBMIT}",
            "method": "POST",
            "content_type": "application/json",
            "request_headers": headers,
            "request_body_schema": {
                "type": "object",
                "required": ["conversation_id", "assignee_key", "task", "mode"],
                "properties": {
                    "conversation_id": conversation,
                    "caller_id": {"type": "string", "dynamic_variable": "caller_id"},
                    "assignee_key": {
                        "type": "string",
                        "enum": [p.key for p in people],
                        "description": "Key of the person the task is for.",
                    },
                    "task": {
                        "type": "string",
                        "description": (
                            "The task in the caller's Egyptian Arabic words, without the "
                            "person's name, keeping time words like بكرة."
                        ),
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["new", "replace_last"],
                        "description": (
                            "new for a new task; replace_last when the caller corrects the "
                            "task you just repeated."
                        ),
                    },
                },
            },
        },
    }
    cancel = {
        "type": "webhook",
        "name": TOOL_CANCEL,
        "description": "Cancel the task you just repeated, when the caller says to drop it.",
        "response_timeout_secs": 5,
        "pre_tool_speech": "off",
        "execution_mode": "immediate",
        "interruption_mode": "allow",
        "api_schema": {
            "url": f"{base}/agent/tools/{TOOL_CANCEL}",
            "method": "POST",
            "content_type": "application/json",
            "request_headers": headers,
            "request_body_schema": {
                "type": "object",
                "required": ["conversation_id"],
                "properties": {"conversation_id": conversation},
            },
        },
    }
    return {TOOL_SUBMIT: submit, TOOL_CANCEL: cancel}


def build_agent(settings: Settings, people: list[Person], tool_ids: list[str]) -> dict:
    return {
        "name": "Voice Monday (Egyptian Arabic)",
        "tags": ["voice-monday"],
        "conversation_config": {
            "agent": {
                "first_message": "ألو",
                "language": "ar",
                "prompt": {
                    "prompt": build_prompt(people),
                    "llm": settings.elevenlabs_agent_llm,
                    "temperature": 0.2,
                    "tool_ids": tool_ids,
                },
            },
            "tts": {
                "model_id": settings.elevenlabs_agent_tts_model,
                "voice_id": settings.elevenlabs_agent_voice_id,
            },
            "turn": {
                "turn_timeout": 7,
                "turn_eagerness": "eager",
                "speculative_turn": True,
            },
            "asr": {"provider": "scribe_realtime", "quality": "high"},
        },
        # Only our bridge (with a signed URL from our API key) may start conversations.
        "platform_settings": {"auth": {"enable_auth": True}},
    }


def build_phone_number(settings: Settings, agent_id: str) -> dict:
    return {
        "phone_number": settings.elevenlabs_sip_number,
        "label": f"Office PBX extension {settings.elevenlabs_sip_number}",
        "provider": "sip_trunk",
        "agent_id": agent_id,
        "inbound_trunk_config": {
            "media_encryption": "allowed",
            "credentials": {
                "username": settings.elevenlabs_sip_username,
                "password": settings.elevenlabs_sip_password,
            },
        },
    }


def missing_settings(settings: Settings) -> list[str]:
    required = {
        "ELEVENLABS_API_KEY": settings.elevenlabs_api_key,
        "ELEVENLABS_AGENT_VOICE_ID": settings.elevenlabs_agent_voice_id,
        "AGENT_TOOL_SECRET": settings.agent_tool_secret,
        "AGENT_PUBLIC_URL": settings.agent_public_url,
        "ELEVENLABS_SIP_USERNAME": settings.elevenlabs_sip_username,
        "ELEVENLABS_SIP_PASSWORD": settings.elevenlabs_sip_password,
    }
    problems = [f"{k} is required" for k, v in required.items() if not str(v).strip()]
    if settings.agent_public_url and not settings.agent_public_url.startswith("https://"):
        problems.append("AGENT_PUBLIC_URL must start with https://")
    return problems


class ElevenLabsAgentSync:
    def __init__(self, settings: Settings, client: httpx.AsyncClient, state_path: Path):
        self.s = settings
        self.client = client
        self.state_path = state_path

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def _save(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    async def _call(self, method: str, path: str, body: dict) -> dict:
        response = await self.client.request(
            method, path, json=body, headers={"xi-api-key": self.s.elevenlabs_api_key}
        )
        if response.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> HTTP {response.status_code}: {response.text}")
        return response.json() if response.content else {}

    async def run(self, directory: PeopleDirectory, *, rotate_secret: bool = False) -> dict:
        state = self._load()
        people = assignable_people(self.s, directory)
        if not people:
            raise RuntimeError("no MONDAY_<NAME>_PERSON_ID configured: nobody can get tasks")

        if rotate_secret or not state.get("secret_id"):
            secret = await self._call(
                "POST",
                "/v1/convai/secrets",
                {"type": "new", "name": SECRET_NAME, "value": f"Bearer {self.s.agent_tool_secret}"},
            )
            state["secret_id"] = secret["secret_id"]
            self._save(state)

        tools = build_tools(self.s, people, state["secret_id"])
        tool_ids = state.setdefault("tool_ids", {})
        for name, config in tools.items():
            if tool_ids.get(name):
                await self._call(
                    "PATCH", f"/v1/convai/tools/{tool_ids[name]}", {"tool_config": config}
                )
            else:
                created = await self._call("POST", "/v1/convai/tools", {"tool_config": config})
                tool_ids[name] = created["id"]
                self._save(state)

        agent_body = build_agent(self.s, people, [tool_ids[n] for n in tools])
        agent_id = state.get("agent_id") or self.s.elevenlabs_agent_id
        if agent_id:
            await self._call("PATCH", f"/v1/convai/agents/{agent_id}", agent_body)
        else:
            agent_id = (await self._call("POST", "/v1/convai/agents/create", agent_body))[
                "agent_id"
            ]
        state["agent_id"] = agent_id
        self._save(state)

        if not (state.get("phone_number_id") or self.s.elevenlabs_phone_number_id):
            phone = await self._call(
                "POST", "/v1/convai/phone-numbers", build_phone_number(self.s, agent_id)
            )
            state["phone_number_id"] = phone["phone_number_id"]
            self._save(state)
        return state
