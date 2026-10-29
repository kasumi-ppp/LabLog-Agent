"""Workspace HTTP contract; the legacy task executor API stays compatible."""
from datetime import date
from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field, field_validator
from .routes import _envelope
from .service import AppError

router = APIRouter(prefix='/api/v1/workspace')
Kind = Literal['daily', 'weekly', 'monthly']


class Period(BaseModel):
    date: date
    kind: Kind = 'daily'


class Manual(BaseModel):
    date: date
    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=6000)
    project: str = Field(default='线下工作', max_length=100)
    status: Literal['planned', 'in_progress', 'implemented_unverified', 'verified_completed', 'blocked', 'unknown'] = 'implemented_unverified'


class Selection(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=200)
    selected: bool
    revision: int


class ReportEdit(Period):
    revision: int | str
    sections: dict[str, str]

    @field_validator('sections')
    @classmethod
    def sections_valid(cls, value):
        if not set(value) <= {'completed', 'progress', 'problems', 'solutions', 'plan'} or any(len(x) > 30000 for x in value.values()):
            raise ValueError('invalid report sections')
        return value


class Submit(Period):
    hash: str = Field(min_length=64, max_length=64)


class ConversationChoice(BaseModel):
    key: str = Field(min_length=1, max_length=180)
    selected: bool | None
    revision: int = Field(ge=1)


class ConversationSelection(BaseModel):
    changes: list[ConversationChoice] = Field(min_length=1, max_length=200)


@router.get('')
def workspace(request: Request, day: date, kind: Kind = 'daily'):
    return _envelope(request.app.state.logs.view(kind, day.isoformat()))


@router.post('/sync')
def sync(payload: Period, request: Request):
    return _envelope(request.app.state.logs.sync(payload.kind, payload.date.isoformat()))


@router.post('/manual')
def manual(payload: Manual, request: Request):
    return _envelope(request.app.state.logs.manual(payload.model_dump(mode='json')))


@router.patch('/selection')
def selection(payload: Selection, request: Request):
    if not request.app.state.logs.repo.select(payload.ids, payload.selected, payload.revision):
        raise AppError('SELECTION_CONFLICT', '任务选择已变化，请刷新后重试。', 409)
    return _envelope({'saved': True})


@router.get('/tasks/{key}/evidence')
def evidence(key: str, request: Request):
    return _envelope(request.app.state.logs.evidence(key))


@router.patch('/report')
def save(payload: ReportEdit, request: Request):
    return _envelope(request.app.state.logs.save(payload.kind, payload.date.isoformat(), payload.revision, payload.sections))


@router.post('/generate')
def generate(payload: Period, request: Request):
    return _envelope(request.app.state.logs.generate(payload.kind, payload.date.isoformat()))


@router.get('/history')
def history(request: Request):
    return _envelope(request.app.state.logs.history())


@router.get('/projects')
def projects(request: Request):
    return _envelope(request.app.state.logs.projects())


@router.get('/conversations')
def conversations(request: Request, project_id: str | None = None, source: Literal['codex', 'workbuddy', 'chatgpt'] | None = None,
                  q: str = '', include_archived: bool = False, include_deleted: bool = False,
                  limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    return _envelope(request.app.state.logs.conversations(project_id=project_id, source=source, q=q,
        include_archived=include_archived, include_deleted=include_deleted, limit=limit, offset=offset))


@router.patch('/conversations/selection')
def select_conversations(payload: ConversationSelection, request: Request):
    return _envelope(request.app.state.logs.select_conversations([c.model_dump() for c in payload.changes]))


@router.get('/conversations/{key}/messages')
def conversation_messages(key: str, request: Request, day: date, kind: Kind = 'daily',
                          limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                          view: Literal['messages', 'tools'] = 'messages'):
    return _envelope(request.app.state.logs.conversation_messages(key, kind=kind, day=day.isoformat(),
                                                               limit=limit, offset=offset, view=view))


@router.post('/submit')
def submit(payload: Submit, request: Request):
    return _envelope(request.app.state.logs.submit(payload.kind, payload.date.isoformat(), payload.hash))
