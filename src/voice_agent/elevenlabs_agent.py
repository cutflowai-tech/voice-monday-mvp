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

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx

from voice_agent.config import Settings
from voice_agent.domain.boards import BoardDirectory
from voice_agent.domain.people import PeopleDirectory, Person
from voice_agent.sarah import (
    ACK_HINTS,
    AUDIO_TAGS,
    DONE_HINTS,
    FILLERS,
    NAME_AR,
    NAME_EN,
    OPENINGS_ANONYMOUS,
    UNKNOWN_CALLER,
    build_prompt,
)

SECRET_NAME = "voice-monday-tool-bearer"  # noqa: S105 (a name, not a value)
TOOL_SUBMIT = "submit_task"
TOOL_CANCEL = "cancel_last"
TOOL_CALL = "call_person"
TOOL_MONDAY = "monday_assistant"
TOOL_PRANK_REVEAL = "prank_reveal"  # client tool, handled by the AudioSocket bridge
PRANK_DIR = "data/prank"


def assignable_people(settings: Settings, directory: PeopleDirectory) -> list[Person]:
    """Only people with a Monday id can receive tasks, so only they are offered."""
    ids = settings.monday_person_ids()
    return [p for p in directory if p.key in ids]


def build_tools(
    settings: Settings,
    people: list[Person],
    secret_id: str,
    callable_people: list[Person] | None = None,
    boards: BoardDirectory | None = None,
    monday_assistant: bool = False,
) -> dict[str, dict]:
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
        # no platform sound: the bridge plays its own, louder keyboard while saving
        "tool_call_sound": None,
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
                            "Short instruction TO the assignee in Egyptian imperative, "
                            "without their name, keeping time words: «اشتري كاميرات بكرة», "
                            "«هات الكهربائي بكرة». The caller's «بيا/ليا» becomes the "
                            "caller's name; «بيكي/ليكي» (said to you) becomes «سارة»."
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
                    **(
                        {
                            "board": {
                                "type": "string",
                                "enum": ["auto", *boards.board_keys()],
                                "description": (
                                    "auto unless the caller named a board or answered which "
                                    "board: "
                                    + "، ".join(
                                        f"{k} = {boards.name_ar(k)}" for k in boards.board_keys()
                                    )
                                ),
                            }
                        }
                        if boards is not None and len(boards.board_keys()) > 1
                        else {}
                    ),
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
    tools = {TOOL_SUBMIT: submit, TOOL_CANCEL: cancel}
    if monday_assistant:
        tools[TOOL_MONDAY] = {
            "type": "webhook",
            "name": TOOL_MONDAY,
            "description": (
                "Ask the Monday back office anything about Monday, or have it change "
                "something there: review a board, find items, who has what, what's late, "
                "change a status/date/owner, add an update, move or archive an item. Returns "
                "a short spoken answer. Not for a simple new task for a team member (that's "
                "submit_task)."
            ),
            "response_timeout_secs": max(10, int(settings.monday_assistant_timeout_seconds) + 10),
            "pre_tool_speech": "force",
            "tool_call_sound": None,  # the bridge plays the keyboard while it works
            "execution_mode": settings.elevenlabs_tool_execution_mode,
            "interruption_mode": "allow",
            "api_schema": {
                "url": f"{base}/agent/tools/{TOOL_MONDAY}",
                "method": "POST",
                "content_type": "application/json",
                "request_headers": headers,
                "request_body_schema": {
                    "type": "object",
                    "required": ["conversation_id", "request"],
                    "properties": {
                        "conversation_id": conversation,
                        "caller_id": {"type": "string", "dynamic_variable": "caller_id"},
                        "request": {
                            "type": "string",
                            "description": (
                                "The caller's request in their own words, complete (board, "
                                "item, person, dates), including what «it/that» refers to "
                                "from earlier in the call. For a reply to the back office's "
                                "question, the caller's answer as said."
                            ),
                        },
                    },
                },
            },
        }
    if callable_people:
        tools[TOOL_CALL] = {
            "type": "webhook",
            "name": TOOL_CALL,
            "description": (
                "Phone a team member's extension to play a friendly prank, when the caller "
                "asks you to («اتصلي بأحمد واعملي فيه مقلب»). Returns at once; the call "
                "happens in the background."
            ),
            "response_timeout_secs": 5,
            "pre_tool_speech": "auto",
            "execution_mode": "immediate",
            "interruption_mode": "allow",
            "api_schema": {
                "url": f"{base}/agent/tools/{TOOL_CALL}",
                "method": "POST",
                "content_type": "application/json",
                "request_headers": headers,
                "request_body_schema": {
                    "type": "object",
                    "required": ["conversation_id", "person_key", "purpose"],
                    "properties": {
                        "conversation_id": conversation,
                        "caller_id": {"type": "string", "dynamic_variable": "caller_id"},
                        "person_key": {
                            "type": "string",
                            "enum": [p.key for p in callable_people],
                            "description": "Key of the team member to call.",
                        },
                        "purpose": {
                            "type": "string",
                            "enum": ["prank"],
                            "description": "Why you're calling.",
                        },
                    },
                },
            },
        }
        tools[TOOL_PRANK_REVEAL] = {
            "type": "client",
            "name": TOOL_PRANK_REVEAL,
            "description": (
                "Prank calls only: play the punchline «خدتك عليه!» with laughter and end the "
                "call. Call it as soon as the person asks what you want, then say nothing."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
            "expects_response": True,
            "response_timeout_secs": 5,
            "pre_tool_speech": "off",
            "execution_mode": "immediate",
        }
    return tools


def build_tts(settings: Settings) -> dict:
    model = settings.elevenlabs_agent_tts_model
    tts: dict[str, Any] = {
        "model_id": model,
        "voice_id": settings.elevenlabs_agent_voice_id,
        "similarity_boost": 0.8,
    }
    if model.startswith("eleven_v3"):
        # expressive mode: laughs, sighs and breaths through audio tags
        tts["expressive_mode"] = True
        tts["suggested_audio_tags"] = [{"tag": t, "description": d} for t, d in AUDIO_TAGS]
        tts["stability"] = 0.5  # v3 "natural"
    else:
        tts["stability"] = 0.4  # more expressive, less monotone
        tts["speed"] = 1.05
    return tts


def asr_keywords(people: list[Person]) -> list[str]:
    words = {p.name_ar for p in people}
    for p in people:
        words.update(a for a in p.aliases if any("\u0600" <= ch <= "\u06ff" for ch in a))
    return sorted(words | {"تاسك", "Monday", "مانداي"})


def build_agent(
    settings: Settings,
    people: list[Person],
    tool_ids: list[str],
    boards: BoardDirectory | None = None,
    monday_assistant: bool = False,
) -> dict:
    return {
        "name": f"{NAME_EN} ({NAME_AR}) - Waset Co task line",
        "tags": ["voice-monday", "sarah"],
        "conversation_config": {
            "agent": {
                # The bridge picks a different greeting per call (sarah.opening_for).
                "first_message": "{{opening}}",
                "language": "ar",
                "dynamic_variables": {
                    "dynamic_variable_placeholders": {
                        "opening": OPENINGS_ANONYMOUS[0],
                        "caller_name": UNKNOWN_CALLER,
                        "caller_id": UNKNOWN_CALLER,
                        "caller_gender": UNKNOWN_CALLER,
                        "call_mode": "inbound",
                        "prank_by": UNKNOWN_CALLER,
                        "ack_hint": ACK_HINTS[0],
                        "done_hint": DONE_HINTS[0],
                    }
                },
                "prompt": {
                    "prompt": build_prompt(
                        people,
                        boards=boards,
                        monday_assistant=monday_assistant,
                        expressive=settings.elevenlabs_agent_tts_model.startswith("eleven_v3"),
                    ),
                    "llm": settings.elevenlabs_agent_llm,
                    # variety in wording; people and tool arguments stay constrained by enums
                    "temperature": 0.6,
                    "tool_ids": tool_ids,
                    # lets her hang up after a goodbye or a prank's punchline
                    "built_in_tools": {
                        "end_call": {
                            "type": "system",
                            "name": "end_call",
                            "description": "",
                            "params": {"system_tool_type": "end_call"},
                        }
                    },
                },
            },
            "tts": build_tts(settings),
            "turn": {
                "turn_timeout": 7,
                "turn_eagerness": "eager",
                "speculative_turn": True,
                # if the model is ever slow, a short human filler instead of silence
                "soft_timeout_config": {
                    "timeout_seconds": 1.5,
                    "message": FILLERS[0],
                    "additional_soft_timeout_messages": list(FILLERS[1:]),
                    "randomize_fillers": True,
                    "use_llm_generated_message": False,
                },
            },
            "asr": {
                "provider": "scribe_realtime",
                "quality": "high",
                # bias recognition towards the team's names and the domain words
                "keywords": asr_keywords(people),
            },
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


def _save_clip(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


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

    def monday_assistant_enabled(self) -> bool:
        """Same conditions as the api's /agent/tools/monday_assistant endpoint."""
        s = self.s
        return bool(s.agent_api_key and s.monday_api_token and s.monday_assistant_allowed())

    def callable_people(self, directory: PeopleDirectory) -> list[Person]:
        """Team members Sarah may phone: outbound enabled and an extension on file."""
        if not self.s.agent_outbound_enabled or not self.s.caller_directory_file:
            return []
        from voice_agent.domain.callers import CallerDirectory

        callers = CallerDirectory.load(self.s.caller_directory_file, {p.key for p in directory})
        return [p for p in directory if callers.extension_for(p.key)]

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

        boards = BoardDirectory.load(self.s.monday_boards_file or None)
        tools = build_tools(
            self.s,
            people,
            state["secret_id"],
            callable_people=self.callable_people(directory),
            boards=boards,
            monday_assistant=self.monday_assistant_enabled(),
        )
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

        agent_body = build_agent(
            self.s,
            people,
            [tool_ids[n] for n in tools],
            boards,
            monday_assistant=self.monday_assistant_enabled(),
        )
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
        if self.s.agent_outbound_enabled:
            await self.render_punchlines(Path(PRANK_DIR))
        return state

    async def render_punchlines(self, folder: Path) -> list[Path]:
        """Punchline clips in the agent's voice with the expressive model (once per voice)."""
        from voice_agent.sarah import PUNCHLINES

        voice = self.s.elevenlabs_agent_voice_id
        made: list[Path] = []
        for gender, texts in PUNCHLINES.items():
            for i, text in enumerate(texts):
                path = folder / f"{voice}-{gender}-{i}.pcm"
                if path.exists():
                    continue
                response = await self.client.post(
                    f"/v1/text-to-speech/{voice}",
                    params={"output_format": "pcm_16000"},
                    headers={"xi-api-key": self.s.elevenlabs_api_key},
                    json={"text": text, "model_id": "eleven_v3"},
                )
                if response.status_code >= 400:
                    raise RuntimeError(f"punchline TTS -> HTTP {response.status_code}")
                await asyncio.to_thread(_save_clip, path, response.content)
                made.append(path)
        return made
