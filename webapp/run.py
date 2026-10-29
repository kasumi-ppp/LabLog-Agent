"""Convenience launcher: python run.py [--port 8000] [--workers 2]"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

WEBAPP = Path(__file__).resolve().parent
BACKEND = WEBAPP / "backend"
sys.path.insert(0, str(WEBAPP))

from backend.app import create_app  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="启动钉钉 AI 日志工作台（WebUI + API + SQLite）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=str(BACKEND / "app.db"), help="数据库文件路径")
    parser.add_argument("--workers", type=int, default=2, help="后台执行线程数")
    parser.add_argument("--config", help="与 WorkBuddy / auto.ps1 使用相同的 LabLog 配置文件")
    args = parser.parse_args()

    import uvicorn

    app = create_app(args.db, workers=args.workers, config_path=args.config)
    print(f"钉钉 AI 日志: http://{args.host}:{args.port}/")
    print(f"API 文档:   http://{args.host}:{args.port}/docs")
    print(f"数据库:     {args.db}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
