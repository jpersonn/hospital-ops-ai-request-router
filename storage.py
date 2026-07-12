"""SQLite audit trail.

Two tables: `requests` (one row per processed request) and `actions` (one row
per remediation step). Persisting the actions separately is what lets the
dashboard answer questions like "how many escalations paused auto-resolution
today" without re-running anything.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from models import ProcessedRequest

DB_PATH = Path(__file__).parent / "audit_log.db"


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS requests (
                request_id   TEXT PRIMARY KEY,
                raw_text     TEXT,
                request_type TEXT,
                urgency      TEXT,
                confidence   REAL,
                sub_topic    TEXT,
                reasoning    TEXT,
                final_status TEXT,
                created_at   TEXT
            );
            CREATE TABLE IF NOT EXISTS actions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT,
                step_name  TEXT,
                status     TEXT,
                detail     TEXT,
                artifact   TEXT,
                timestamp  TEXT,
                FOREIGN KEY (request_id) REFERENCES requests(request_id)
            );
            """
        )
        # Lightweight migration: databases created before the reasoning column
        # existed get it added in place. SQLite has no ADD COLUMN IF NOT EXISTS,
        # so the duplicate-column error on already-migrated DBs is expected.
        try:
            conn.execute("ALTER TABLE requests ADD COLUMN reasoning TEXT")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise


def save_request(pr: ProcessedRequest) -> None:
    with _connect() as conn:
        c = pr.classification
        conn.execute(
            """INSERT OR REPLACE INTO requests
               (request_id, raw_text, request_type, urgency, confidence,
                sub_topic, reasoning, final_status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pr.request_id, pr.raw_text, c.request_type.value, c.urgency.value,
             c.confidence, c.sub_topic, c.reasoning, pr.final_status, pr.created_at),
        )
        conn.execute("DELETE FROM actions WHERE request_id = ?", (pr.request_id,))
        for a in pr.actions:
            conn.execute(
                """INSERT INTO actions
                   (request_id, step_name, status, detail, artifact, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (pr.request_id, a.step_name, a.status, a.detail, a.artifact, a.timestamp),
            )


def all_requests() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM requests ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_actions_for(request_id: str) -> list[dict]:
    """Ordered audit-trail rows (one per remediation step) for one request."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM actions WHERE request_id = ? ORDER BY id",
            (request_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def summary_by_type() -> dict[str, int]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT request_type, COUNT(*) n FROM requests GROUP BY request_type"
        ).fetchall()
        return {r["request_type"]: r["n"] for r in rows}


def summary_by_status() -> dict[str, int]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT final_status, COUNT(*) n FROM requests GROUP BY final_status"
        ).fetchall()
        return {r["final_status"]: r["n"] for r in rows}


def reset_db() -> None:
    """Wipe the log -- handy for a clean demo run.

    Deletes rows rather than unlinking the file. On Windows, SQLite can hold
    a file handle open even after a `with` block exits, so deleting the file
    itself raises PermissionError (WinError 32) while the app is running.
    Clearing tables in place works cross-platform and avoids that entirely.
    """
    init_db()  # ensure tables exist first
    with _connect() as conn:
        conn.execute("DELETE FROM actions")
        conn.execute("DELETE FROM requests")