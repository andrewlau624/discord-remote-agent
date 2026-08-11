"""SQLite store mapping a Discord channel to an agent session.

One row per channel, kept across restarts so /resume can reload a session by id.
We keep the cwd because Claude Code reloads a session's transcript from the same
working directory it was created in.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass


@dataclass
class SessionRow:
    channel_id: int
    provider: str
    session_id: str | None
    cwd: str
    title: str | None
    status: str
    created_at: float
    updated_at: float


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    channel_id INTEGER PRIMARY KEY,
    provider   TEXT    NOT NULL,
    session_id TEXT,
    cwd        TEXT    NOT NULL,
    title      TEXT,
    status     TEXT    NOT NULL DEFAULT 'active',
    created_at REAL    NOT NULL,
    updated_at REAL    NOT NULL
);
"""


class Store:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _row(r: sqlite3.Row) -> SessionRow:
        return SessionRow(
            channel_id=r["channel_id"],
            provider=r["provider"],
            session_id=r["session_id"],
            cwd=r["cwd"],
            title=r["title"],
            status=r["status"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )

    def upsert(
        self,
        channel_id: int,
        provider: str,
        cwd: str,
        session_id: str | None = None,
        title: str | None = None,
        status: str = "active",
    ) -> None:
        now = time.time()
        self._conn.execute(
            """
            INSERT INTO sessions
                (channel_id, provider, session_id, cwd, title, status,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                provider   = excluded.provider,
                session_id = excluded.session_id,
                cwd        = excluded.cwd,
                title      = excluded.title,
                status     = excluded.status,
                updated_at = excluded.updated_at
            """,
            (channel_id, provider, session_id, cwd, title, status, now, now),
        )
        self._conn.commit()

    def set_session_id(self, channel_id: int, session_id: str) -> None:
        self._conn.execute(
            "UPDATE sessions SET session_id = ?, updated_at = ? WHERE channel_id = ?",
            (session_id, time.time(), channel_id),
        )
        self._conn.commit()

    def set_status(self, channel_id: int, status: str) -> None:
        self._conn.execute(
            "UPDATE sessions SET status = ?, updated_at = ? WHERE channel_id = ?",
            (status, time.time(), channel_id),
        )
        self._conn.commit()

    def get(self, channel_id: int) -> SessionRow | None:
        cur = self._conn.execute(
            "SELECT * FROM sessions WHERE channel_id = ?", (channel_id,)
        )
        row = cur.fetchone()
        return self._row(row) if row else None

    def find_by_session_id(self, session_id: str) -> SessionRow | None:
        cur = self._conn.execute(
            "SELECT * FROM sessions WHERE session_id = ? ORDER BY updated_at DESC LIMIT 1",
            (session_id,),
        )
        row = cur.fetchone()
        return self._row(row) if row else None

    def list_all(self) -> list[SessionRow]:
        cur = self._conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC")
        return [self._row(r) for r in cur.fetchall()]

    def delete(self, channel_id: int) -> None:
        self._conn.execute("DELETE FROM sessions WHERE channel_id = ?", (channel_id,))
        self._conn.commit()
