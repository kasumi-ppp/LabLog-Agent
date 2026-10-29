"""Initialize (or reset) the SQLite database from the command line.

Usage:
    python init_db.py                 # create schema if missing
    python init_db.py --seed          # also insert demo tasks
    python init_db.py --reset --seed  # DELETE the db file first, then recreate
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

WEBAPP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WEBAPP_DIR))

from backend.database import Database  # noqa: E402
from backend.repository import TaskRepository  # noqa: E402

DB_PATH = Path(__file__).resolve().parent / "app.db"


def seed(repo: TaskRepository) -> None:
    demos = [
        {"title": "示例：回显任务", "task_type": "echo",
         "description": "最小的生命周期演示：创建后立即被后台执行器认领并完成。",
         "payload": {"hello": "world"}},
        {"title": "示例：三步模拟作业", "task_type": "simulate_work",
         "description": "观察进度条从 0 走到 100，以及事件时间线的记录。",
         "payload": {"steps": 3, "step_seconds": 1.0}},
        {"title": "示例：会失败的作业", "task_type": "simulate_work",
         "description": "第 2 步故意抛错，演示 failed 状态与错误信息展示；可点重试。",
         "payload": {"steps": 3, "step_seconds": 0.5, "fail_at": 2}},
        {"title": "示例：求和", "task_type": "sum_numbers",
         "description": "校验数字并求和；payload 非法时任务失败。",
         "payload": {"numbers": [1, 2, 3, 4, 5]}},
    ]
    for demo in demos:
        task = repo.insert(demo)
        repo.log_event(task["id"], "created", to_status="pending",
                       detail={"task_type": task["task_type"], "priority": task["priority"]})
    print(f"  已写入 {len(demos)} 条示例任务")


def main() -> int:
    parser = argparse.ArgumentParser(description="初始化任务系统 SQLite 数据库")
    parser.add_argument("--db", default=str(DB_PATH), help="数据库文件路径（默认 backend/app.db）")
    parser.add_argument("--seed", action="store_true", help="写入演示任务")
    parser.add_argument("--reset", action="store_true", help="先删除已有数据库文件再重建")
    args = parser.parse_args()

    path = Path(args.db)
    if args.reset and path.exists():
        for suffix in ("", "-wal", "-shm"):
            extra = Path(str(path) + suffix)
            if extra.exists():
                extra.unlink()
        print(f"已删除旧数据库：{path}")

    db = Database(path)
    counts = db.stats_counts()
    total = sum(counts.values())
    print(f"数据库就绪：{path}")
    print(f"  schema 版本已确认；当前任务数：{total}（按状态：{counts}）")

    if args.seed and total == 0:
        seed(TaskRepository(db))
    elif args.seed:
        print("  库中已有任务，跳过写入示例（如需重建请加 --reset）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
