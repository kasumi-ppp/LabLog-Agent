"""Data access layer: every SQL statement in the application lives here.

The repository returns plain dicts shaped for the API, never sqlite3.Row,
so upper layers carry zero storage coupling.
"""

from __future__ import annotations

from .database import Database, dumps, loads, utc_now

TASK_COLUMNS = (
    "id, title, description, task_type, payload, status, priority, progress, "
    "progress_message, result, error, cancel_requested, created_at, updated_at, "
    "started_at, finished_at, revision"
)

PRIORITY_RANK = ("high", "normal", "low")


def _to_task(row) -> dict | None:
    if row is None:
        return None
    task = dict(row)
    task["payload"] = loads(task.get("payload"), {})
    task["result"] = loads(task.get("result"))
    task["cancel_requested"] = bool(task.get("cancel_requested"))
    return task


def _to_event(row) -> dict:
    event = dict(row)
    event["detail"] = loads(event.get("detail"), {})
    return event


class TaskRepository:
    def __init__(self, db: Database):
        self.db = db

    # -- reads ---------------------------------------------------------------

    def get(self, task_id: int) -> dict | None:
        row = self.db.query_one(
            f"SELECT {TASK_COLUMNS} FROM tasks WHERE id = ?", (task_id,)
        )
        return _to_task(row)

    def list(
        self,
        *,
        status: str | None = None,
        task_type: str | None = None,
        keyword: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        where, params = ["1=1"], []
        if status:
            where.append("status = ?")
            params.append(status)
        if task_type:
            where.append("task_type = ?")
            params.append(task_type)
        if keyword:
            where.append("(title LIKE ? OR description LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        clause = " AND ".join(where)
        total = self.db.query_one(
            f"SELECT COUNT(*) AS n FROM tasks WHERE {clause}", tuple(params)
        )["n"]
        rows = self.db.query(
            f"SELECT {TASK_COLUMNS} FROM tasks WHERE {clause} "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            tuple(params) + (limit, offset),
        )
        return [_to_task(r) for r in rows], total

    def next_pending(self) -> dict | None:
        """Oldest pending task, highest priority first (executor claim order)."""
        placeholders = ",".join("?" for _ in PRIORITY_RANK)
        order = "CASE priority " + " ".join(
            f"WHEN '{p}' THEN {i}" for i, p in enumerate(PRIORITY_RANK)
        ) + " ELSE 9 END"
        row = self.db.query_one(
            f"SELECT {TASK_COLUMNS} FROM tasks WHERE status = 'pending' "
            f"ORDER BY {order}, created_at ASC, id ASC LIMIT 1"
        )
        return _to_task(row)

    def stats_counts(self) -> dict[str, int]:
        return self.db.stats_counts()

    def type_counts(self) -> list[dict]:
        rows = self.db.query(
            "SELECT task_type, COUNT(*) AS n FROM tasks GROUP BY task_type ORDER BY n DESC"
        )
        return [{"task_type": r["task_type"], "count": r["n"]} for r in rows]

    # -- writes ----------------------------------------------------------------

    def insert(self, data: dict) -> dict:
        now = utc_now()
        self.db.write(
            "INSERT INTO tasks(title, description, task_type, payload, status, "
            "priority, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                data["title"], data.get("description", ""), data["task_type"],
                dumps(data.get("payload", {})), "pending", data.get("priority", "normal"),
                now, now,
            ),
        )
        row = self.db.query_one("SELECT last_insert_rowid() AS id")
        return self.get(row["id"])

    def log_event(
        self,
        task_id: int,
        event_type: str,
        *,
        from_status: str | None = None,
        to_status: str | None = None,
        detail: dict | None = None,
    ) -> None:
        self.db.write(
            "INSERT INTO task_events(task_id, event_type, from_status, to_status, "
            "detail, created_at) VALUES(?,?,?,?,?,?)",
            (task_id, event_type, from_status, to_status, dumps(detail or {}), utc_now()),
        )

    def list_events(self, task_id: int, limit: int = 200) -> list[dict]:
        rows = self.db.query(
            "SELECT id, task_id, event_type, from_status, to_status, detail, created_at "
            "FROM task_events WHERE task_id = ? ORDER BY id ASC LIMIT ?",
            (task_id, limit),
        )
        return [_to_event(r) for r in rows]

    def delete_events(self, task_id: int) -> None:
        self.db.write("DELETE FROM task_events WHERE task_id = ?", (task_id,))

    def delete(self, task_id: int) -> int:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
            cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            return cur.rowcount

    # -- field-level updates used by the service and executor -----------------

    def update_fields(self, task_id: int, fields: dict, *, bump_revision: bool = False) -> None:
        if not fields:
            return
        assignments, params = [], []
        for column, value in fields.items():
            assignments.append(f"{column} = ?")
            if column in {"payload", "result"} and value is not None and not isinstance(value, str):
                value = dumps(value)
            params.append(value)
        if bump_revision:
            assignments.append("revision = revision + 1")
        assignments.append("updated_at = ?")
        params.append(utc_now())
        params.append(task_id)
        self.db.write(f"UPDATE tasks SET {', '.join(assignments)} WHERE id = ?", tuple(params))

    def claim_for_run(self, task_id: int, expected_revision: int) -> bool:
        """Atomically move pending -> running; returns False if someone else won.

        The revision is bumped here so the executor's later finish() call can
        guard on the new value. The event row is inserted inside the same
        transaction — calling log_event() here would re-acquire the non-reentrant
        write lock we already hold and deadlock.
        """
        now = utc_now()
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status = 'running', started_at = ?, updated_at = ?, "
                "progress = 0, progress_message = '', cancel_requested = 0, "
                "revision = revision + 1 "
                "WHERE id = ? AND status = 'pending' AND revision = ?",
                (now, now, task_id, expected_revision),
            )
            if cur.rowcount != 1:
                return False
            conn.execute(
                "INSERT INTO task_events(task_id, event_type, from_status, to_status, "
                "detail, created_at) VALUES(?,?,?,?,?,?)",
                (task_id, "status_changed", "pending", "running", "{}", utc_now()),
            )
        return True

    def request_cancel(self, task_id: int) -> int:
        self.db.write(
            "UPDATE tasks SET cancel_requested = 1, updated_at = ? "
            "WHERE id = ? AND status IN ('pending', 'running')",
            (utc_now(), task_id),
        )
        return self.db.query_one(
            "SELECT cancel_requested AS c FROM tasks WHERE id = ?", (task_id,)
        )["c"]

    def finish(self, task_id: int, status: str, *, result=None, error: str | None = None,
               progress: int | None = None, revision: int) -> dict | None:
        """Write a terminal state. progress=None keeps the last reported value —
        a task cancelled at 30% must not be rewritten to 100%."""
        now = utc_now()
        fields = {
            "status": status, "finished_at": now,
            "result": dumps(result) if result is not None else None,
            "error": error, "cancel_requested": 0,
        }
        if progress is not None:
            fields["progress"] = max(0, min(100, progress))
        assignments = ", ".join(f"{k} = ?" for k in fields)
        changed = self.db.write(
            f"UPDATE tasks SET {assignments}, updated_at = ?, revision = revision + 1 "
            "WHERE id = ? AND revision = ?",
            tuple(fields.values()) + (now, task_id, revision),
        )
        if changed != 1:
            return None
        task = self.get(task_id)
        self.log_event(
            task_id, "status_changed",
            from_status="running", to_status=status,
            detail={"result_present": result is not None, "error": error},
        )
        return task

    def set_progress(self, task_id: int, progress: int, message: str) -> None:
        self.db.write(
            "UPDATE tasks SET progress = ?, progress_message = ?, updated_at = ? "
            "WHERE id = ? AND status = 'running'",
            (max(0, min(100, progress)), message[:500], utc_now(), task_id),
        )

    def requeue(self, task_id: int, *, revision: int) -> dict | None:
        """failed -> pending (retry path)."""
        now = utc_now()
        changed = self.db.write(
            "UPDATE tasks SET status = 'pending', progress = 0, progress_message = '', "
            "result = NULL, error = NULL, cancel_requested = 0, started_at = NULL, "
            "finished_at = NULL, updated_at = ?, revision = revision + 1 "
            "WHERE id = ? AND status = 'failed' AND revision = ?",
            (now, task_id, revision),
        )
        if changed != 1:
            return None
        task = self.get(task_id)
        self.log_event(task_id, "status_changed", from_status="failed", to_status="pending",
                       detail={"reason": "retry"})
        return task
