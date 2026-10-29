"""Business rules and task lifecycle state machine.

The service layer is the API's only entry point into the system. It validates
input, enforces legal status transitions and revision (optimistic locking), and
composes repository calls. Routes never touch the repository directly.
"""

from __future__ import annotations

from . import handlers
from .database import TERMINAL_STATUSES, TRANSITIONS, utc_now
from .repository import TaskRepository

PRIORITIES = ("low", "normal", "high")


class AppError(Exception):
    """Domain error carrying an API error code and HTTP status."""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


class TaskService:
    def __init__(self, repo: TaskRepository, executor=None):
        self.repo = repo
        self.executor = executor  # only used to wake workers after create/retry

    # -- helpers -------------------------------------------------------------

    def _require(self, task_id: int) -> dict:
        task = self.repo.get(task_id)
        if task is None:
            raise AppError("TASK_NOT_FOUND", f"task {task_id} does not exist", 404)
        return task

    @staticmethod
    def _assert_transition(current: str, target: str) -> None:
        if target not in TRANSITIONS.get(current, set()):
            raise AppError(
                "INVALID_TRANSITION",
                f"cannot move task from '{current}' to '{target}'", 409,
            )

    # -- commands ------------------------------------------------------------

    def create(self, data: dict) -> dict:
        title = (data.get("title") or "").strip()
        if not title:
            raise AppError("VALIDATION_ERROR", "title is required", 422)
        if len(title) > 200:
            raise AppError("VALIDATION_ERROR", "title must be at most 200 characters", 422)

        task_type = data.get("task_type") or "echo"
        if task_type not in handlers.known_types():
            raise AppError(
                "UNKNOWN_TASK_TYPE",
                f"unknown task type '{task_type}'; known: {', '.join(handlers.known_types())}",
                422,
            )

        priority = data.get("priority") or "normal"
        if priority not in PRIORITIES:
            raise AppError("VALIDATION_ERROR", f"priority must be one of {PRIORITIES}", 422)

        payload = data.get("payload")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise AppError("VALIDATION_ERROR", "payload must be a JSON object", 422)

        description = data.get("description") or ""
        if not isinstance(description, str) or len(description) > 5000:
            raise AppError("VALIDATION_ERROR", "description must be a string of at most 5000 chars", 422)

        task = self.repo.insert({
            "title": title, "description": description, "task_type": task_type,
            "payload": payload, "priority": priority,
        })
        self.repo.log_event(task["id"], "created", to_status="pending",
                            detail={"task_type": task_type, "priority": priority})
        if self.executor is not None:
            self.executor.notify_enqueued()
        return task

    def update(self, task_id: int, data: dict, *, expected_revision: int | None) -> dict:
        task = self._require(task_id)
        # pending: change before it runs; failed: fix the payload, then retry —
        # the standard "fix and re-run" workflow.
        if task["status"] not in {"pending", "failed"}:
            raise AppError(
                "INVALID_TRANSITION",
                f"only pending or failed tasks can be edited (current status: {task['status']})", 409,
            )
        if expected_revision is not None and expected_revision != task["revision"]:
            raise AppError(
                "REVISION_CONFLICT",
                f"revision mismatch: expected {expected_revision}, current {task['revision']}", 409,
            )

        fields: dict = {}
        if data.get("title") is not None:
            title = str(data["title"]).strip()
            if not title:
                raise AppError("VALIDATION_ERROR", "title cannot be empty", 422)
            fields["title"] = title
        if data.get("description") is not None:
            if not isinstance(data["description"], str) or len(data["description"]) > 5000:
                raise AppError("VALIDATION_ERROR", "invalid description", 422)
            fields["description"] = data["description"]
        if data.get("priority") is not None:
            if data["priority"] not in PRIORITIES:
                raise AppError("VALIDATION_ERROR", f"priority must be one of {PRIORITIES}", 422)
            fields["priority"] = data["priority"]
        if data.get("payload") is not None:
            if not isinstance(data["payload"], dict):
                raise AppError("VALIDATION_ERROR", "payload must be a JSON object", 422)
            fields["payload"] = data["payload"]
        if not fields:
            raise AppError("VALIDATION_ERROR", "no editable fields supplied", 422)

        self.repo.update_fields(task_id, fields, bump_revision=True)
        self.repo.log_event(task_id, "updated", detail={"fields": sorted(fields)})
        return self._require(task_id)

    def cancel(self, task_id: int) -> dict:
        task = self._require(task_id)
        self._assert_transition(task["status"], "cancelled")
        if task["status"] == "pending":
            self.repo.update_fields(
                task_id,
                {"status": "cancelled", "finished_at": utc_now(), "cancel_requested": 0},
                bump_revision=True,
            )
            self.repo.log_event(task_id, "status_changed",
                                from_status="pending", to_status="cancelled",
                                detail={"reason": "cancelled before start"})
            return self._require(task_id)
        # running: cooperative — the handler observes the flag at its next checkpoint
        requested = self.repo.request_cancel(task_id)
        if not requested:
            raise AppError("INVALID_TRANSITION", "task is no longer running", 409)
        self.repo.log_event(task_id, "cancel_requested", detail={"note": "worker will stop at next checkpoint"})
        return self._require(task_id)

    def retry(self, task_id: int) -> dict:
        task = self._require(task_id)
        self._assert_transition(task["status"], "pending")
        retried = self.repo.requeue(task_id, revision=task["revision"])
        if retried is None:
            raise AppError("REVISION_CONFLICT", "task changed concurrently, retry again", 409)
        if self.executor is not None:
            self.executor.notify_enqueued()
        return retried

    def delete(self, task_id: int) -> dict:
        task = self._require(task_id)
        if task["status"] not in TERMINAL_STATUSES:
            raise AppError(
                "INVALID_TRANSITION",
                f"only terminal tasks can be deleted (current: {task['status']})", 409,
            )
        self.repo.delete(task_id)
        return {"deleted": task_id}

    # -- queries ---------------------------------------------------------------

    def get(self, task_id: int) -> dict:
        return self._require(task_id)

    def list(self, **kwargs) -> dict:
        items, total = self.repo.list(**kwargs)
        return {"items": items, "total": total,
                "limit": kwargs.get("limit", 50), "offset": kwargs.get("offset", 0)}

    def events(self, task_id: int) -> list[dict]:
        self._require(task_id)
        return self.repo.list_events(task_id)

    def stats(self) -> dict:
        return {
            "by_status": self.repo.stats_counts(),
            "by_type": self.repo.type_counts(),
        }
