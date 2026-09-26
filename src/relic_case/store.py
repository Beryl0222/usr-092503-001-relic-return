"""SQLite 事件存储：事件追加与外部标识登记的幂等保障。"""

from __future__ import annotations

import json
import sqlite3
import threading

from .events import Event, EventType

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    case_id TEXT NOT NULL,
    type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    role TEXT,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    voided_by TEXT
);
CREATE TABLE IF NOT EXISTS external_registry (
    external_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    type TEXT NOT NULL
);
"""


class DuplicateExternalId(Exception):
    """同一外部标识已被登记，用于兜底并发写入。"""


class EventStore:
    """事件只增不改；撤销通过追加 EVENT_VOIDED 并标记目标事件完成。"""

    def __init__(self, path: str = ":memory:"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        with self._conn:
            self._conn.executescript(_SCHEMA)

    def append(self, event: Event, *, external_id: str | None = None) -> None:
        """在同一事务内写入事件并（可选）登记外部标识。"""

        with self._lock, self._conn:
            try:
                if external_id is not None:
                    self._conn.execute(
                        "INSERT INTO external_registry(external_id, case_id, event_id, type)"
                        " VALUES (?, ?, ?, ?)",
                        (external_id, event.case_id, event.event_id, event.type.value),
                    )
                cursor = self._conn.execute(
                    "INSERT INTO events(event_id, case_id, type, actor_id, role, occurred_at, payload_json)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.event_id,
                        event.case_id,
                        event.type.value,
                        event.actor_id,
                        event.role,
                        event.occurred_at,
                        json.dumps(event.payload, ensure_ascii=False),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateExternalId(external_id) from exc
            event.seq = cursor.lastrowid

    def mark_voided(self, void_event: Event, target_event_id: str) -> None:
        """追加撤销事件、标记目标事件、释放其占用的外部标识。"""

        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO events(event_id, case_id, type, actor_id, role, occurred_at, payload_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    void_event.event_id,
                    void_event.case_id,
                    void_event.type.value,
                    void_event.actor_id,
                    void_event.role,
                    void_event.occurred_at,
                    json.dumps(void_event.payload, ensure_ascii=False),
                ),
            )
            void_event.seq = cursor.lastrowid
            self._conn.execute(
                "UPDATE events SET voided_by = ? WHERE event_id = ?",
                (void_event.event_id, target_event_id),
            )
            self._conn.execute(
                "DELETE FROM external_registry WHERE event_id = ?", (target_event_id,)
            )

    def lookup_external(self, external_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT external_id, case_id, event_id, type FROM external_registry"
                " WHERE external_id = ?",
                (external_id,),
            ).fetchone()
        return dict(row) if row else None

    def load_all(self) -> list[Event]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, event_id, case_id, type, actor_id, role, occurred_at,"
                " payload_json, voided_by FROM events ORDER BY seq"
            ).fetchall()
        return [
            Event(
                event_id=row["event_id"],
                case_id=row["case_id"],
                type=EventType(row["type"]),
                actor_id=row["actor_id"],
                role=row["role"],
                occurred_at=row["occurred_at"],
                payload=json.loads(row["payload_json"]),
                seq=row["seq"],
                voided_by=row["voided_by"],
            )
            for row in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
