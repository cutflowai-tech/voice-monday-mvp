"""Webhook tools called by the hosted ElevenLabs agent.

Exposed publicly through the Cloudflare Tunnel (only ``/agent/tools/*``), so every
request must carry ``Authorization: Bearer $AGENT_TOOL_SECRET``. With no secret
configured the endpoints do not exist (404).
"""

from __future__ import annotations

import hmac
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from voice_agent.agent_tools.window import TaskWindow

CONVERSATION_PATTERN = r"^[A-Za-z0-9_\-:.]{1,128}$"

router = APIRouter(prefix="/agent/tools", tags=["agent tools"])


class SubmitTaskRequest(BaseModel):
    conversation_id: str = Field(pattern=CONVERSATION_PATTERN)
    assignee_key: str = Field(min_length=1, max_length=64)
    task: str = Field(min_length=1, max_length=255)
    mode: Literal["new", "replace_last"] = "new"
    caller_id: str | None = Field(default=None, max_length=64)


class CancelLastRequest(BaseModel):
    conversation_id: str = Field(pattern=CONVERSATION_PATTERN)


class CallPersonRequest(BaseModel):
    conversation_id: str = Field(pattern=CONVERSATION_PATTERN)
    person_key: str = Field(min_length=1, max_length=64)
    purpose: Literal["prank"] = "prank"
    caller_id: str | None = Field(default=None, max_length=64)


def _window(request: Request, authorization: str | None) -> TaskWindow:
    window: TaskWindow | None = getattr(request.app.state, "task_window", None)
    secret = request.app.state.container.settings.agent_tool_secret
    if window is None or not secret:
        raise HTTPException(status_code=404)
    expected = f"Bearer {secret}".encode()
    if not authorization or not hmac.compare_digest(authorization.encode(), expected):
        raise HTTPException(status_code=401, detail="invalid token")
    return window


@router.post("/submit_task")
async def submit_task(
    body: SubmitTaskRequest,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    window = _window(request, authorization)
    return await window.submit(
        conversation_id=body.conversation_id,
        assignee_key=body.assignee_key.strip().lower(),
        task=body.task,
        mode=body.mode,
        caller_id=body.caller_id,
    )


@router.post("/cancel_last")
async def cancel_last(
    body: CancelLastRequest,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    window = _window(request, authorization)
    return await window.cancel_last(conversation_id=body.conversation_id)


@router.post("/call_person")
async def call_person(
    body: CallPersonRequest,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _window(request, authorization)  # same bearer check as the task tools
    outbound = getattr(request.app.state, "outbound", None)
    if outbound is None:
        return {"status": "unavailable"}
    return await outbound.request(
        target_key=body.person_key.strip().lower(),
        purpose=body.purpose,
        caller_id=body.caller_id,
    )
