"""Durable job state in SQLite.

Every state transition is a guarded UPDATE (``WHERE status = ...``) so two
workers can never both own a job, and each transition appends a row to
``job_events`` for auditing (``GET /jobs/{id}/events``).
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from typing import Any

from voice_agent.db import Database
from voice_agent.jobs.models import Job, JobEvent, JobSpec, JobStatus

Clock = Callable[[], float]


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        idempotency_key=row["idempotency_key"],
        status=JobStatus(row["status"]),
        connector=row["connector"],
        operation=row["operation"],
        payload=json.loads(row["payload_json"]),
        intent=json.loads(row["intent_json"]),
        transcript=row["transcript"],
        session_id=row["session_id"],
        call_id=row["call_id"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        next_attempt_at=row["next_attempt_at"],
        lease_expires_at=row["lease_expires_at"],
        prior_attempt_uncertain=bool(row["prior_attempt_uncertain"]),
        last_error=row["last_error"],
        result=json.loads(row["result_json"]) if row["result_json"] else None,
        external_ref=row["external_ref"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
    )


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class JobStore:
    def __init__(self, db: Database, clock: Clock = time.time):
        self.db = db
        self.clock = clock

    # ------------------------------------------------------------------ create/read
    def create(
        self,
        spec: JobSpec,
        *,
        idempotency_key: str,
        transcript: str,
        intent: dict[str, Any],
        max_attempts: int,
        session_id: str | None = None,
        call_id: str | None = None,
    ) -> tuple[Job, bool]:
        """Persist a new job. Returns (job, created). Duplicate keys return the original."""
        now = self.clock()
        job_id = uuid.uuid4().hex
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing:
                return _row_to_job(existing), False
            conn.execute(
                """
                INSERT INTO jobs (id, idempotency_key, status, connector, operation,
                    payload_json, intent_json, transcript, session_id, call_id,
                    attempts, max_attempts, next_attempt_at, created_at, updated_at)
                VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    idempotency_key,
                    spec.connector,
                    spec.operation,
                    _dumps(spec.payload),
                    _dumps(intent),
                    transcript,
                    session_id,
                    call_id,
                    max_attempts,
                    now,
                    now,
                    now,
                ),
            )
            self._event(conn, job_id, JobStatus.QUEUED, 0, "accepted", {"key": idempotency_key})
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row), True

    def get(self, job_id: str) -> Job | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def get_by_key(self, idempotency_key: str) -> Job | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        return _row_to_job(row) if row else None

    def events(self, job_id: str) -> list[JobEvent]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM job_events WHERE job_id = ? ORDER BY id", (job_id,)
            ).fetchall()
        return [
            JobEvent(
                id=r["id"],
                job_id=r["job_id"],
                at=r["at"],
                status=JobStatus(r["status"]),
                attempt=r["attempt"],
                message=r["message"],
                data=json.loads(r["data_json"]) if r["data_json"] else None,
            )
            for r in rows
        ]

    # ------------------------------------------------------------------ transitions
    def claim(self, job_id: str, lease_seconds: float) -> Job | None:
        """Take ownership of a due job (or one whose previous owner's lease expired)."""
        now = self.clock()
        with self.db.transaction() as conn:
            row = conn.execute(
                """
                UPDATE jobs SET
                    prior_attempt_uncertain = CASE WHEN status = 'processing'
                        THEN 1 ELSE prior_attempt_uncertain END,
                    status = 'processing',
                    attempts = attempts + 1,
                    lease_expires_at = ?,
                    updated_at = ?
                WHERE id = ? AND (
                    (status = 'queued' AND next_attempt_at <= ?)
                    OR (status = 'processing' AND lease_expires_at < ?)
                )
                RETURNING *
                """,
                (now + lease_seconds, now, job_id, now, now),
            ).fetchone()
            if row is None:
                return None
            job = _row_to_job(row)
            self._event(conn, job.id, JobStatus.PROCESSING, job.attempts, "attempt started")
        return job

    def complete(self, job: Job, *, result: dict[str, Any], external_ref: str | None) -> Job | None:
        now = self.clock()
        return self._transition(
            job,
            JobStatus.COMPLETED,
            "completed",
            "result_json = ?, external_ref = ?, completed_at = ?, last_error = NULL, "
            "lease_expires_at = NULL",
            (_dumps(result), external_ref, now),
            data={"external_ref": external_ref},
        )

    def schedule_retry(
        self, job: Job, *, error: str, delay_seconds: float, uncertain: bool
    ) -> Job | None:
        next_at = self.clock() + delay_seconds
        return self._transition(
            job,
            JobStatus.QUEUED,
            f"retry scheduled in {delay_seconds:.1f}s: {error}",
            "last_error = ?, next_attempt_at = ?, lease_expires_at = NULL, "
            "prior_attempt_uncertain = ?",
            (error, next_at, int(uncertain or job.prior_attempt_uncertain)),
            data={"delay_seconds": round(delay_seconds, 3), "uncertain": uncertain},
        )

    def fail(self, job: Job, *, error: str) -> Job | None:
        return self._transition(
            job,
            JobStatus.FAILED,
            f"permanent failure: {error}",
            "last_error = ?, completed_at = ?, lease_expires_at = NULL",
            (error, self.clock()),
        )

    def dead_letter(self, job: Job, *, error: str) -> Job | None:
        return self._transition(
            job,
            JobStatus.DEAD_LETTER,
            f"retries exhausted: {error}",
            "last_error = ?, completed_at = ?, lease_expires_at = NULL",
            (error, self.clock()),
        )

    def _transition(
        self,
        job: Job,
        status: JobStatus,
        message: str,
        set_sql: str,
        params: tuple,
        data: dict[str, Any] | None = None,
    ) -> Job | None:
        """Move a job we own (status=processing, same attempt) to ``status``."""
        now = self.clock()
        with self.db.transaction() as conn:
            row = conn.execute(
                f"UPDATE jobs SET status = ?, updated_at = ?, {set_sql} "  # noqa: S608
                "WHERE id = ? AND status = 'processing' AND attempts = ? RETURNING *",
                (status.value, now, *params, job.id, job.attempts),
            ).fetchone()
            if row is None:
                return None  # lease lost; another worker owns the job now
            self._event(conn, job.id, status, job.attempts, message, data)
        return _row_to_job(row)

    # ------------------------------------------------------------------ recovery
    def find_recoverable(self, *, stale_after_seconds: float, limit: int = 100) -> list[str]:
        """Jobs that should be in the queue but may have been lost (crash, Redis loss)."""
        now = self.clock()
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT id FROM jobs
                WHERE (status = 'queued' AND next_attempt_at <= ? AND updated_at <= ?)
                   OR (status = 'processing' AND lease_expires_at < ?)
                ORDER BY next_attempt_at LIMIT ?
                """,
                (now, now - stale_after_seconds, now, limit),
            ).fetchall()
        return [r["id"] for r in rows]

    def status_counts(self) -> dict[str, int]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
            return {r["status"]: r["n"] for r in rows}

    def _event(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        status: JobStatus,
        attempt: int,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO job_events (job_id, at, status, attempt, message, data_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                job_id,
                self.clock(),
                status.value,
                attempt,
                message[:1000],
                _dumps(data) if data else None,
            ),
        )
