"""Structured intents produced by an :class:`IntentParser`.

The top-level fields (``action``, ``assignee``, ``task``) are the stable contract
shared by the parser, acknowledgment generator, command handlers and API. Extra
spoken/linguistic detail lives in :class:`IntentDetails` so new intents can add
slots without breaking existing consumers.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Action(StrEnum):
    CREATE_TASK = "create_task"
    REMIND = "remind"
    SCHEDULE_MEETING = "schedule_meeting"
    SEND_MESSAGE = "send_message"
    END_CALL = "end_call"
    SMALL_TALK = "small_talk"
    CONFIRM = "confirm"
    DENY = "deny"


Gender = Literal["m", "f"]


class IntentDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assignee_spoken: str | None = None
    """Arabic name as it should be spoken back, e.g. "محمد"."""
    assignee_key: str | None = None
    """Directory key used by connectors to resolve external ids, e.g. "mohamed"."""
    assignee_gender: Gender | None = None
    action_phrase: str | None = None
    """Verb phrase as spoken by the caller, e.g. "يشتري هارد للسيرفر"."""
    subject: str | None = None
    """Object of meetings/messages, e.g. "تقرير المبيعات"."""
    when: str | None = None
    """Free-text time expression, e.g. "بكرة" or "يوم الخميس"."""


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Action
    assignee: str | None = Field(default=None, max_length=100)
    task: str | None = Field(default=None, max_length=255)
    details: IntentDetails = Field(default_factory=IntentDetails)

    def public_dict(self) -> dict:
        """Compact representation used by the API and job storage."""
        data = self.model_dump(mode="json", exclude_none=True)
        details = {k: v for k, v in data.pop("details", {}).items() if v is not None}
        if details:
            data["details"] = details
        return data


ParseStatus = Literal["ok", "incomplete", "unknown"]


class ParseResult(BaseModel):
    """Outcome of parsing one utterance."""

    status: ParseStatus
    intent: Intent | None = None
    missing: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    normalized_text: str = ""

    @classmethod
    def unknown(cls, normalized_text: str = "") -> ParseResult:
        return cls(status="unknown", normalized_text=normalized_text)
