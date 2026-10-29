"""Background executor: claims pending tasks and runs them in worker threads.

Decoupling notes:
- The executor talks ONLY to the repository and the handler registry. It never
  sees HTTP concepts, and the API layer never spawns work directly — creating
  a task is just a row insert; this loop picks it up.
- Claiming is atomic (claim_for_run guards on status='pending' AND revision),
  so two workers can never run the same task.
"""

from __future__ import annotations

import logging
import threading
import time

from . import handlers
from .database import Database
from .repository import TaskRepository

log = logging.getLogger("webapp.executor")


class Executor:
    def __init__(self, db: Database, *, workers: int = 2, poll_interval: float = 0.5):
        self.repo = TaskRepository(db)
        # workers=0 is legal: no queue draining happens (used by tests and by
        # API-only deployments that drive tasks externally).
        self.workers = max(0, workers)
        self.poll_interval = poll_interval
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        for index in range(self.workers):
            thread = threading.Thread(target=self._loop, name=f"task-worker-{index}", daemon=True)
            thread.start()
            self._threads.append(thread)
        log.info("executor started with %d worker(s)", self.workers)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        log.info("executor stopped")

    def notify_enqueued(self) -> None:
        """Wake workers immediately after a task is created (latency helper)."""
        self._wake.set()

    # -- worker loop ---------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self._run_one()
            except Exception:  # never let the loop die
                log.exception("worker loop error")
                worked = False
            if worked:
                continue  # drain the queue before sleeping again
            self._wake.wait(timeout=self.poll_interval)
            self._wake.clear()

    def _run_one(self) -> bool:
        task = self.repo.next_pending()
        if task is None:
            return False
        if not self.repo.claim_for_run(task["id"], task["revision"]):
            return False  # another worker claimed it first
        self._execute(task)
        return True

    # -- execution -------------------------------------------------------------

    def _execute(self, task: dict) -> None:
        task_id = task["id"]
        handler = handlers.get_handler(task["task_type"])

        def on_progress(percent: int, message: str) -> None:
            self.repo.set_progress(task_id, percent, message)

        def cancelled() -> bool:
            row = self.repo.db.query_one(
                "SELECT cancel_requested AS c FROM tasks WHERE id = ?", (task_id,)
            )
            return bool(row and row["c"])

        ctx = handlers.HandlerContext(task_id, on_progress, cancelled)

        if handler is None:
            self.repo.finish(task_id, "failed", error=f"unknown task type: {task['task_type']}",
                             revision=task["revision"] + 1)
            return

        try:
            result = handler(ctx, task.get("payload") or {})
        except handlers.TaskCancelled:
            self.repo.finish(task_id, "cancelled", error="cancelled during execution",
                             revision=task["revision"] + 1)
        except Exception as exc:  # handler failure -> failed task with message
            log.warning("task %d failed: %s: %s", task_id, type(exc).__name__, exc)
            self.repo.finish(task_id, "failed", error=f"{type(exc).__name__}: {exc}",
                             revision=task["revision"] + 1)
        else:
            self.repo.finish(task_id, "succeeded", result=result, progress=100,
                             revision=task["revision"] + 1)
