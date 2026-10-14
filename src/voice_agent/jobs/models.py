from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class JobStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


TERMINAL_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.DEAD_LETTER})


class JobSpec(BaseModel):
    """What to execute: produced by a command handler, consumed by a connector."""

    connector: str = Field(min_length=1, max_length=64)
    operation: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any]


class Job(BaseModel):
    id: str
    idempotency_key: str
    status: JobStatus
    connector: str
    operation: str
    payload: dict[str, Any]
    intent: dict[str, Any]
    transcript: str
    session_id: str | None = None
    call_id: str | None = None
    attempts: int = 0
    max_attempts: int
    next_attempt_at: float
    lease_expires_at: float | None = None
    prior_attempt_uncertain: bool = False
    last_error: str | None = None
    result: dict[str, Any] | None = None
    external_ref: str | None = None
    created_at: float
    updated_at: float
    completed_at: float | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def public_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "status": self.status.value,
            "idempotency_key": self.idempotency_key,
            "connector": self.connector,
            "operation": self.operation,
            "intent": self.intent,
            "transcript": self.transcript,
            "session_id": self.session_id,
            "call_id": self.call_id,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "next_attempt_at": iso(self.next_attempt_at) if not self.is_terminal else None,
            "last_error": self.last_error,
            "result": self.result,
            "external_ref": self.external_ref,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
            "completed_at": iso(self.completed_at) if self.completed_at else None,
        }


class JobEvent(BaseModel):
    id: int
    job_id: str
    at: float
    status: JobStatus
    attempt: int
    message: str
    data: dict[str, Any] | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "at": iso(self.at),
            "status": self.status.value,
            "attempt": self.attempt,
            "message": self.message,
            "data": self.data,
        }


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="milliseconds")
