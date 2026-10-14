"""SQLite access and schema migrations.

SQLite is the durable source of truth for job state and the call audit log.
Each operation opens a short-lived connection (safe across threads and
processes); WAL mode lets the API, worker and ARI processes share one file.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK (
                status IN ('queued', 'processing', 'completed', 'failed', 'dead_letter')
            ),
            connector TEXT NOT NULL,
            operation TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            intent_json TEXT NOT NULL,
            transcript TEXT NOT NULL,
            session_id TEXT,
            call_id TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL,
            next_attempt_at REAL NOT NULL,
            lease_expires_at REAL,
            prior_attempt_uncertain INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            result_json TEXT,
            external_ref TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            completed_at REAL
        );
        CREATE INDEX idx_jobs_status_next ON jobs (status, next_attempt_at);
        CREATE INDEX idx_jobs_session ON jobs (session_id);

        CREATE TABLE job_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES jobs (id),
            at REAL NOT NULL,
            status TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            message TEXT NOT NULL,
            data_json TEXT
        );
        CREATE INDEX idx_job_events_job ON job_events (job_id, id);

        -- Call-session audit log. Deliberately separate from job state.
        CREATE TABLE utterances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            call_id TEXT,
            turn INTEGER NOT NULL,
            transcript TEXT NOT NULL,
            parse_status TEXT,
            intent_json TEXT,
            reply_kind TEXT,
            reply_text TEXT,
            job_id TEXT,
            created_at REAL NOT NULL
        );
        CREATE INDEX idx_utterances_session ON utterances (session_id, turn);
        """,
    ),
]


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 10000")
            conn.execute("PRAGMA synchronous = FULL")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Write transaction that takes the lock up front (no upgrade deadlocks)."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def migrate(self) -> int:
        """Apply pending migrations. Returns the resulting schema version."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
            )
        with self.transaction() as conn:
            current = conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()[0]
            for version, sql in MIGRATIONS:
                if version > current:
                    for statement in filter(None, (s.strip() for s in sql.split(";"))):
                        conn.execute(statement)
                    conn.execute(
                        "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                        (version, time.time()),
                    )
                    current = version
        return current

    def check(self) -> bool:
        with self.connect() as conn:
            conn.execute("SELECT 1 FROM jobs LIMIT 1").fetchall()
        return True
