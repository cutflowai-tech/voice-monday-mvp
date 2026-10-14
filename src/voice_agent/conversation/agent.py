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


class ReviewTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assignee_key: str
    task: str = Field(min_length=2, max_length=255)


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["draft", "clarify", "discard"]
    tasks: list[ReviewTask] = Field(max_length=5)
    question: str | None = Field(max_length=160)


class StructuredTaskAgent:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        directory: PeopleDirectory,
        provider: Literal["openai", "openrouter"] = "openai",
        timeout_seconds: float = 8.0,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.model = model
        self.provider = provider
        self.directory = directory
        self._key = api_key
        self._client = httpx.AsyncClient(
            base_url=(
                base_url
                or (
                    "https://openrouter.ai/api/v1"
                    if provider == "openrouter"
                    else "https://api.openai.com/v1"
                )
            ).rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds, connect=min(3.0, timeout_seconds)),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def decide_review(
        self, text: str, current: list[ReviewTask] | None = None
    ) -> ReviewDecision:
        """Extract or revise up to five tasks; the caller confirms before persistence."""
        people = [{"key": p.key, "name": p.name_ar, "aliases": p.aliases} for p in self.directory]
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["action", "tasks", "question"],
            "properties": {
                "action": {"type": "string", "enum": ["draft", "clarify", "discard"]},
                "tasks": {
                    "type": "array",
                    "maxItems": 5,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["assignee_key", "task"],
                        "properties": {
                            "assignee_key": {
                                "type": "string",
                                "enum": [p["key"] for p in people],
                            },
                            "task": {"type": "string"},
                        },
                    },
                },
                "question": {"type": ["string", "null"]},
            },
        }
        instructions = (
            "You help Egyptian Arabic callers draft Monday tasks, never execute them. "
            "Extract ALL distinct tasks in the utterance in order, preserving names, actions, "
            "negation, quantities, and date words exactly. Map each assignee to a directory key. "
            "When current drafts are supplied, apply the caller's correction to the named or "
            "numbered task and return the FULL revised list, retaining unchanged drafts. "
            "If a correction cannot be located or any assignee/task is unclear, return clarify "
            "with a short Egyptian Arabic question and no tasks. Never invent details or people. "
            "A request to tell a person to do something is a task for that person. "
            "Do not interpret a bare refusal as a correction."
        )
        user_input = json.dumps(
            {
                "utterance": text,
                "current": [t.model_dump() for t in current or []],
                "people": people,
            },
            ensure_ascii=False,
        )
        format_spec = {
            "type": "json_schema",
            "name": "review_decision",
            "strict": True,
            "schema": schema,
        }
        if self.provider == "openrouter":
            response = await self._client.post(
                "/chat/completions",
                headers={"Authorization": f"Bearer {self._key}"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": instructions},
                        {"role": "user", "content": user_input},
                    ],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {k: v for k, v in format_spec.items() if k != "type"},
                    },
                    "provider": {"require_parameters": True},
                },
            )
            response.raise_for_status()
            content = (response.json().get("choices") or [{}])[0].get("message", {}).get("content")
        else:
            response = await self._client.post(
                "/responses",
                headers={"Authorization": f"Bearer {self._key}"},
                json={
                    "model": self.model,
                    "store": False,
                    "instructions": instructions,
                    "input": user_input,
                    "text": {"format": format_spec},
                },
            )
            response.raise_for_status()
            data = response.json()
            if data.get("status") != "completed":
                raise ValueError("review response incomplete")
            outputs = [
                item.get("text")
                for output in data.get("output", [])
                for item in output.get("content", [])
                if item.get("type") == "output_text"
            ]
            content = outputs[0] if len(outputs) == 1 else None
        if not isinstance(content, str):
            raise ValueError("agent returned no review output")
        decision = ReviewDecision.model_validate_json(content)
        if any(not self.directory.get(t.assignee_key) for t in decision.tasks):
            raise ValueError("review returned an unverified assignee")
        if decision.action == "draft" and not decision.tasks:
            raise ValueError("review returned an empty draft")
        return decision

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
        instructions = (
            "You assist Egyptian Arabic callers ONLY with creating Monday tasks. "
            "Return one structured decision. Preserve quantities, negation and corrections. "
            "Use only directory keys. Treat a correction in the same utterance as changing "
            "the current draft, retaining unchanged details. A new request starts a new draft. "
            "Submit when assignee and task are clear; do not ask for optional specifications "
            "or routine confirmation. Never invent requirements. For unsupported requests "
            "return unsupported. A bare 'استني' means wait. A bare 'لا' during an "
            "unsubmitted draft means discard. Keep question brief in Egyptian Arabic. "
            "Never claim a Monday item was created."
        )
        user_input = json.dumps(
            {"utterance": text, "draft": draft, "people": people}, ensure_ascii=False
        )
        format_spec = {
            "type": "json_schema",
            "name": "task_decision",
            "strict": True,
            "schema": schema,
        }
        if self.provider == "openrouter":
            path = "/chat/completions"
            body = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": user_input},
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {k: v for k, v in format_spec.items() if k != "type"},
                },
                "provider": {"require_parameters": True},
            }
        else:
            path = "/responses"
            body = {
                "model": self.model,
                "store": False,
                "instructions": instructions,
                "input": user_input,
                "text": {"format": format_spec},
            }
        response = await self._client.post(
            path, headers={"Authorization": f"Bearer {self._key}"}, json=body
        )
        response.raise_for_status()
        data = response.json()
        if self.provider == "openrouter":
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content")
            if not isinstance(content, str):
                raise ValueError("agent returned no structured output")
            decision = AgentDecision.model_validate_json(content)
            if decision.assignee_key and not self.directory.get(decision.assignee_key):
                raise ValueError("agent returned an unverified assignee")
            return decision
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
