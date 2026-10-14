"""Narrow structured conversation decision; application code owns execution."""

from __future__ import annotations

import json
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from voice_agent.conversation.engine import Pending
from voice_agent.domain.people import PeopleDirectory


class AgentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["submit", "clarify", "discard", "unsupported", "wait"]
    assignee_key: str | None
    task: str | None = Field(max_length=255)
    question: str | None = Field(max_length=160)


class StructuredTaskAgent:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        directory: PeopleDirectory,
        timeout_seconds: float = 8.0,
        base_url: str = "https://api.openai.com/v1",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.model = model
        self.directory = directory
        self._key = api_key
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds, connect=min(3.0, timeout_seconds)),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def decide(self, text: str, pending: Pending | None) -> AgentDecision:
        people = [{"key": p.key, "name": p.name_ar, "aliases": p.aliases} for p in self.directory]
        draft = None
        if pending and pending.kind == "clarify":
            draft = {
                "assignee_key": pending.intent.details.assignee_key,
                "task": pending.intent.task,
            }
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["action", "assignee_key", "task", "question"],
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["submit", "clarify", "discard", "unsupported", "wait"],
                },
                "assignee_key": {
                    "type": ["string", "null"],
                    "enum": [*[p["key"] for p in people], None],
                },
                "task": {"type": ["string", "null"]},
                "question": {"type": ["string", "null"]},
            },
        }
        body = {
            "model": self.model,
            "store": False,
            "instructions": (
                "You assist Egyptian Arabic callers ONLY with creating Monday tasks. "
                "Return one structured decision. Preserve quantities, negation and corrections. "
                "Use only directory keys. Treat a correction in the same utterance as changing "
                "the current draft, retaining unchanged details. A new request starts a new draft. "
                "Submit when assignee and task are clear; do not ask for optional specifications "
                "or routine confirmation. Never invent requirements. For unsupported requests "
                "return unsupported. A bare 'استني' means wait. A bare 'لا' during an "
                "unsubmitted draft means discard. Keep question brief in Egyptian Arabic. "
                "Never claim a Monday item was created."
            ),
            "input": json.dumps(
                {"utterance": text, "draft": draft, "people": people}, ensure_ascii=False
            ),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "task_decision",
                    "strict": True,
                    "schema": schema,
                }
            },
        }
        response = await self._client.post(
            "/responses", headers={"Authorization": f"Bearer {self._key}"}, json=body
        )
        response.raise_for_status()
        data = response.json()
        if data.get("status") != "completed":
            raise ValueError("agent response incomplete")
        content = [
            item.get("text")
            for output in data.get("output", [])
            for item in output.get("content", [])
            if item.get("type") == "output_text"
        ]
        if len(content) != 1:
            raise ValueError("agent returned no unique structured output")
        decision = AgentDecision.model_validate_json(content[0])
        if decision.assignee_key and not self.directory.get(decision.assignee_key):
            raise ValueError("agent returned an unverified assignee")
        return decision
