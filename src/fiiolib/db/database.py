"""SQLite connection management, schema migrations and small query helpers."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

SCHEMA_VERSION = 1
SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ core
    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA busy_timeout = 30000")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.executemany(sql, rows)

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> Any:
        row = self.one(sql, params)
        return row[0] if row is not None else None

    # -------------------------------------------------------------- migrations
    def migrate(self) -> None:
        self.conn.executescript(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        current = int(self.scalar("SELECT COALESCE(MAX(version), 0) FROM schema_migrations") or 0)
        if current < 1:
            script = SCHEMA_FILE.read_text(encoding="utf-8")
            self.conn.executescript(script)
            self.conn.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (1)")
            current = 1
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema version {current} is newer than this application supports ({SCHEMA_VERSION})"
            )

    # ------------------------------------------------------------- utilities
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, utcnow()),
        )

    def get_preference(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM user_preferences WHERE key = ?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            return row["value"]

    def set_preference(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO user_preferences(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, json.dumps(value), utcnow()),
        )

    def record_log(self, level: str, component: str, message: str, context: Any = None) -> None:
        self.execute(
            "INSERT INTO logs(level, component, message, context) VALUES (?, ?, ?, ?)",
            (level, component, message, json.dumps(context, default=str) if context else None),
        )

    def start_workflow(self, workflow: str) -> int:
        cur = self.execute(
            "INSERT INTO workflow_runs(workflow, status) VALUES (?, 'running')", (workflow,)
        )
        return int(cur.lastrowid)

    def finish_workflow(self, run_id: int, status: str, summary: Any = None, error: str | None = None) -> None:
        self.execute(
            "UPDATE workflow_runs SET finished_at = ?, status = ?, summary = ?, error = ? WHERE id = ?",
            (utcnow(), status, json.dumps(summary, default=str) if summary is not None else None, error, run_id),
        )

    def backup(self, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(destination)
        try:
            self.conn.backup(target)
        finally:
            target.close()
        return destination

    def integrity_check(self) -> dict[str, Any]:
        result = str(self.scalar("PRAGMA integrity_check"))
        return {"ok": result.lower() == "ok", "result": result}
