"""SQLite connection management and schema.

The database layer is the only place that knows about SQL. Everything above
this module (repository -> service -> routes) speaks in plain dicts and
dataclasses, which is what keeps the API layer decoupled from storage.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT    NOT NULL,
    description     TEXT    NOT NULL DEFAULT '',
    task_type       TEXT    NOT NULL DEFAULT 'echo',
    payload         TEXT    NOT NULL DEFAULT '{}',
    status          TEXT    NOT NULL DEFAULT 'pending',
    priority        TEXT    NOT NULL DEFAULT 'normal',
    progress        INTEGER NOT NULL DEFAULT 0,
    progress_message TEXT   NOT NULL DEFAULT '',
    result          TEXT,
    error           TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    revision        INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_tasks_status  ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);

CREATE TABLE IF NOT EXISTS task_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    event_type  TEXT    NOT NULL,
    from_status TEXT,
    to_status   TEXT,
    detail      TEXT    NOT NULL DEFAULT '{}',
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id, id);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# States a task can be in, and which transitions are legal. Kept here so the
# storage layer, the executor and the API agree on one source of truth.
STATUSES = ("pending", "running", "succeeded", "failed", "cancelled")
TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")
TRANSITIONS = {
    "pending": {"running", "cancelled"},
    "running": {"succeeded", "failed", "cancelled"},
    "failed": {"pending"},  # retry
    "succeeded": set(),
    "cancelled": set(),
}


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Database:
    """A thin, thread-safe wrapper around one SQLite file.

    SQLite is happy with many readers and one writer; we serialize writes with
    a lock and open every connection with WAL + busy_timeout so the web
    threads and the background executor never fight over the file.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._conn_lock = threading.Lock()
        self._conns: list[sqlite3.Connection] = []
        self._local = threading.local()
        self.initialize()

    # -- connections ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        # check_same_thread=False: a connection may be *closed* from a
        # different thread than the one that opened it (see close_all);
        # concurrent *use* is already serialized by _write_lock / short reads.
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        with self._conn_lock:
            self._conns.append(conn)
        return conn

    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    # -- read / write helpers ------------------------------------------------

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.connection().execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.connection().execute(sql, params).fetchone()

    def write(self, sql: str, params: tuple = ()) -> int:
        """Run one mutating statement; returns affected rowcount."""
        with self._write_lock:
            cur = self.connection().execute(sql, params)
            return cur.rowcount

    def transaction(self):
        """Context manager for multi-statement atomic writes."""
        return _Transaction(self)

    # -- lifecycle -----------------------------------------------------------

    def close_all(self) -> None:
        """Close every connection opened by this Database (any thread).

        Called on application shutdown, *after* the executor threads have been
        joined. Safe to call multiple times.
        """
        with self._conn_lock:
            conns = self._conns
            self._conns = []
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass
        # Drop cached references so later use reopens a fresh connection
        # instead of touching a closed one.
        self._local = threading.local()

    # -- schema --------------------------------------------------------------

    def initialize(self) -> None:
        # The *keeper* connection stays open for the whole Database lifetime.
        # In WAL mode the -shm/-wal side files are deleted by whichever
        # connection closes last; if that happens while other connections are
        # cached (thread-local), those stale connections lose their shared
        # mapping and every subsequent write fails with
        # "attempt to write a readonly database". Keeping one connection open
        # pins the WAL files so cached connections never go stale.
        self._keeper = self._connect()
        conn = self._keeper
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('version', ?) "
            "ON CONFLICT(key) DO NOTHING",
            (str(SCHEMA_VERSION),),
        )

    def stats_counts(self) -> dict[str, int]:
        rows = self.query("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")
        counts = {status: 0 for status in STATUSES}
        for row in rows:
            counts[row["status"]] = row["n"]
        return counts


class _Transaction:
    def __init__(self, db: Database):
        self.db = db

    def __enter__(self) -> sqlite3.Connection:
        self.db._write_lock.acquire()
        self.conn = self.db.connection()
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is None:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute("ROLLBACK")
        finally:
            self.db._write_lock.release()
        return False


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads(text: str | None, default=None):
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default
