"""SQLite 持久化：事件只追加，任何状态都可由事件流重放得到。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq                INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id            TEXT NOT NULL,
    event_type         TEXT NOT NULL,
    actor_id           TEXT NOT NULL,
    actor_role         TEXT NOT NULL,
    idempotency_key    TEXT,
    material_refs_json TEXT NOT NULL DEFAULT '[]',
    payload_json       TEXT NOT NULL,
    note               TEXT,
    reversal_of_seq    INTEGER,
    occurred_at        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_idempotency
    ON events(case_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_case_seq ON events(case_id, seq);
"""


class EventStore:
    """封装 SQLite 连接；调用方负责事务边界与重放。"""

    def __init__(self, db_path: str | Path | None = ":memory:"):
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def begin(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self._conn.execute("COMMIT")

    def rollback(self) -> None:
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.OperationalError as exc:
            if "no transaction is active" not in str(exc):
                raise

    def append(
        self,
        *,
        case_id: str,
        event_type: str,
        actor_id: str,
        actor_role: str,
        payload: dict,
        occurred_at: str,
        idempotency_key: str | None = None,
        material_refs: Iterable[dict] | None = None,
        note: str | None = None,
        reversal_of_seq: int | None = None,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO events (case_id, event_type, actor_id, actor_role,
                                idempotency_key, material_refs_json, payload_json,
                                note, reversal_of_seq, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                case_id,
                event_type,
                actor_id,
                actor_role,
                idempotency_key,
                json.dumps(list(material_refs or []), ensure_ascii=False),
                json.dumps(payload, ensure_ascii=False),
                note,
                reversal_of_seq,
                occurred_at,
            ),
        )
        return int(cur.lastrowid)

    def load_case(self, case_id: str) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM events WHERE case_id=? ORDER BY seq", (case_id,)
            )
        )

    def list_cases(self) -> list[str]:
        return [
            row[0]
            for row in self._conn.execute(
                "SELECT DISTINCT case_id FROM events ORDER BY case_id"
            )
        ]

    def close(self) -> None:
        self._conn.close()
