"""Request/response contracts (pydantic v2)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class TaskCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field("", max_length=5000)
    task_type: str = Field("echo")
    priority: str = Field("normal", pattern="^(low|normal|high)$")
    payload: dict = Field(default_factory=dict)


class TaskUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=5000)
    priority: str | None = Field(None, pattern="^(low|normal|high)$")
    payload: dict | None = None
    revision: int | None = Field(None, description="乐观锁：期望的当前版本号")


class Envelope(BaseModel):
    """Unified response shape required by the API contract:
    {"request_id": "...", "data": {...}, "error": null}"""

    request_id: str
    data: object = None
    error: dict | None = None
