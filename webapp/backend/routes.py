"""REST routes. Protocol concerns only: parse request -> call service -> wrap
result in the unified envelope. No SQL, no business rules here.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Request

from .schemas import TaskCreate, TaskUpdate
from .service import AppError

router = APIRouter(prefix="/api/v1")


def _envelope(data, status_code: int = 200) -> dict:
    return {"request_id": uuid.uuid4().hex[:16], "data": data, "error": None}


@router.get("/health")
def health() -> dict:
    return _envelope({"status": "ok"})


@router.get("/stats")
def stats(request: Request) -> dict:
    return _envelope(request.app.state.service.stats())


@router.get("/task-types")
def task_types() -> dict:
    from . import handlers

    return _envelope({"types": handlers.known_types()})


@router.post("/tasks", status_code=201)
def create_task(payload: TaskCreate, request: Request) -> dict:
    task = request.app.state.service.create(payload.model_dump())
    return _envelope(task, 201)


@router.get("/tasks")
def list_tasks(
    request: Request,
    status: str | None = Query(None),
    task_type: str | None = Query(None),
    keyword: str | None = Query(None, description="匹配标题或描述"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    page = request.app.state.service.list(
        status=status, task_type=task_type, keyword=keyword, limit=limit, offset=offset
    )
    return _envelope(page)


@router.get("/tasks/{task_id}")
def get_task(task_id: int, request: Request) -> dict:
    return _envelope(request.app.state.service.get(task_id))


@router.get("/tasks/{task_id}/events")
def get_task_events(task_id: int, request: Request) -> dict:
    return _envelope({"items": request.app.state.service.events(task_id)})


@router.patch("/tasks/{task_id}")
def update_task(task_id: int, payload: TaskUpdate, request: Request) -> dict:
    body = payload.model_dump(exclude_none=True)
    task = request.app.state.service.update(
        task_id, body, expected_revision=payload.revision
    )
    return _envelope(task)


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: int, request: Request) -> dict:
    return _envelope(request.app.state.service.cancel(task_id))


@router.post("/tasks/{task_id}/retry")
def retry_task(task_id: int, request: Request) -> dict:
    return _envelope(request.app.state.service.retry(task_id))


@router.delete("/tasks/{task_id}")
def delete_task(task_id: int, request: Request) -> dict:
    return _envelope(request.app.state.service.delete(task_id))
