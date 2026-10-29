"""Application assembly: database -> repository -> service -> executor -> routes.

Run with:
    python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
(from the webapp directory), or simply `python run.py`.
"""

from __future__ import annotations

import logging
import uuid
import os
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .database import Database
from .executor import Executor
from .repository import TaskRepository
from .routes import router
from .service import AppError, TaskService

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from .log_repository import LogRepository
from .log_service import LogService
from .log_routes import router as log_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

BASE_DIR = Path(__file__).resolve().parent.parent  # webapp/
DEFAULT_DB = Path(__file__).resolve().parent / "app.db"


def create_app(db_path: str | Path = DEFAULT_DB, *, workers: int = 2, config_path=None) -> FastAPI:
    app = FastAPI(
        title="钉钉 AI 日志 API",
        version="1.0.0",
        description="三层架构任务系统：WebUI -> RESTful API -> SQLite",
    )

    db = Database(db_path)
    repo = TaskRepository(db)
    executor = Executor(db, workers=workers)
    service = TaskService(repo, executor)

    app.state.db = db
    app.state.repo = repo
    app.state.executor = executor
    app.state.service = service
    project = BASE_DIR.parent
    if config_path is None:
        config_path = os.environ.get('LABLOG_CONFIG')
        if not config_path:
            config_path = next((p for p in (project / 'config/config.auto.yaml', project / 'config/config.local.yaml') if p.is_file()), None)
    app.state.logs = LogService(LogRepository(db), config_path)
    app.include_router(router)
    app.include_router(log_router)

    @app.middleware('http')
    async def protect_workspace(request: Request, call_next):
        from urllib.parse import urlsplit
        origin = request.headers.get('origin')
        if request.method not in {'GET', 'HEAD', 'OPTIONS'} and origin and urlsplit(origin).netloc != request.headers.get('host'):
            return JSONResponse(status_code=403, content={'error': {'code': 'ORIGIN_REJECTED', 'message': '拒绝跨站写入请求。'}})
        token = os.environ.get('LABLOG_WEB_TOKEN')
        if not token and request.url.hostname not in {'127.0.0.1', 'localhost', '::1', 'testserver'}:
            return JSONResponse(status_code=403, content={'error': {'code': 'HOST_REJECTED', 'message': '拒绝未授权的主机名。'}})
        if request.url.path.startswith('/api/'):
            supplied = request.headers.get('authorization', '').removeprefix('Bearer ')
            remote = request.client and request.client.host not in {'127.0.0.1', '::1', 'testclient'}
            if (token and not secrets.compare_digest(token, supplied)) or (remote and not token):
                return JSONResponse(status_code=401, content={'error': {'code': 'UNAUTHORIZED', 'message': '请输入服务器访问令牌。'}})
        try:
            return await call_next(request)
        except Exception:
            logging.exception('Workspace request failed')
            return JSONResponse(status_code=500, content={'request_id': uuid.uuid4().hex[:16], 'data': None,
                'error': {'code': 'OPERATION_FAILED', 'message': '操作未完成，原有数据已保留。请查看服务器诊断日志。'}})

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"request_id": uuid.uuid4().hex[:16], "data": None,
                     "error": {"code": exc.code, "message": exc.message}},
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        executor.start()
        try:
            yield
        finally:
            executor.stop()
            db.close_all()  # join workers first, then release every SQLite handle

    app.router.lifespan_context = lifespan

    frontend = BASE_DIR / "frontend"
    if (frontend / "index.html").is_file():
        app.mount("/static", StaticFiles(directory=str(frontend)), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(frontend / "index.html")

    return app


app = create_app()
