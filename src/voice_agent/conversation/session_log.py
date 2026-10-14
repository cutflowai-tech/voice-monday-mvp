"""Append-only audit log of every utterance in a call session.

Stored in the ``utterances`` table, separate from job state: losing or pruning
call history never affects job execution.
"""

from __future__ import annotations

import json
import time
from typing import Any

from voice_agent.db import Database


class SessionLog:
    def __init__(self, db: Database):
        self.db = db

    def record(
        self,
        *,
        session_id: str,
        call_id: str | None,
        turn: int,
        transcript: str,
        parse_status: str | None,
        intent: dict[str, Any] | None,
        reply_kind: str,
        reply_text: str,
        job_id: str | None,
    ) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO utterances (session_id, call_id, turn, transcript, parse_status,
                    intent_json, reply_kind, reply_text, job_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    call_id,
                    turn,
                    transcript,
                    parse_status,
                    json.dumps(intent, ensure_ascii=False) if intent else None,
                    reply_kind,
                    reply_text,
                    job_id,
                    time.time(),
                ),
            )

    def for_session(self, session_id: str) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM utterances WHERE session_id = ? ORDER BY turn, id", (session_id,)
            ).fetchall()
        return [dict(r) for r in rows]
