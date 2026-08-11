"""SQLite store binding a session thread to an agent session.

One row per thread (a thread id is a channel id). The row holds the session id so
a binding survives restarts; cwd and titles come from the provider's own session
data, not from here. Stopping a session deletes the row.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass


@dataclass
class Pin:
    channel_id: int
    provider: str
    session_id: str | None
    created_at: float
    updated_at: float


_SCHEMA = """
CREATE TABLE IF NOT EXISTS pins (
    channel_id INTEGER PRIMARY KEY,
    provider   TEXT    NOT NULL,
    session_id TEXT,
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
    def _pin(r: sqlite3.Row) -> Pin:
        return Pin(
            channel_id=r["channel_id"],
            provider=r["provider"],
            session_id=r["session_id"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )

    def pin(self, channel_id: int, provider: str, session_id: str | None) -> None:
        now = time.time()
        self._conn.execute(
            """
            INSERT INTO pins (channel_id, provider, session_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                provider   = excluded.provider,
                session_id = excluded.session_id,
                updated_at = excluded.updated_at
            """,
            (channel_id, provider, session_id, now, now),
        )
        self._conn.commit()

    def set_session_id(self, channel_id: int, session_id: str) -> None:
        self._conn.execute(
            "UPDATE pins SET session_id = ?, updated_at = ? WHERE channel_id = ?",
            (session_id, time.time(), channel_id),
        )
        self._conn.commit()

    def get(self, channel_id: int) -> Pin | None:
        cur = self._conn.execute("SELECT * FROM pins WHERE channel_id = ?", (channel_id,))
        row = cur.fetchone()
        return self._pin(row) if row else None

    def find_by_session_id(self, session_id: str) -> Pin | None:
        cur = self._conn.execute(
            "SELECT * FROM pins WHERE session_id = ? LIMIT 1", (session_id,)
        )
        row = cur.fetchone()
        return self._pin(row) if row else None

    def list_all(self) -> list[Pin]:
        cur = self._conn.execute("SELECT * FROM pins ORDER BY updated_at DESC")
        return [self._pin(r) for r in cur.fetchall()]

    def unpin(self, channel_id: int) -> None:
        self._conn.execute("DELETE FROM pins WHERE channel_id = ?", (channel_id,))
        self._conn.commit()
