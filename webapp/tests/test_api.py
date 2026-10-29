"""API 层端到端测试：真实 SQLite（tmp_path）+ TestClient + 真实后台执行器。

覆盖任务生命周期：创建 -> 执行 -> 成功/失败/取消，以及错误码、乐观锁与解耦约束。
运行方式（项目根或 webapp 目录）：
    TMPDIR/TEMP/TMP 指向项目内临时目录后: python -m pytest webapp/tests -q
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

WEBAPP = Path(__file__).resolve().parent.parent
BACKEND = WEBAPP / "backend"
sys.path.insert(0, str(WEBAPP))

from backend.app import create_app  # noqa: E402
from backend.database import Database  # noqa: E402
from backend.executor import Executor  # noqa: E402
from backend.handlers import HandlerContext, register  # noqa: E402
from backend.repository import TaskRepository  # noqa: E402
from backend.service import TaskService  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    app = create_app(tmp_path / "test.db", workers=1)
    with TestClient(app) as test_client:  # context manager triggers startup/shutdown
        yield test_client


@pytest.fixture()
def pending_client(tmp_path):
    """workers=0：执行器不启动线程，任务永远停在 pending，便于确定性地测试
    编辑/取消等「仅 pending 可用」的操作（不受真实执行竞态影响）。"""
    app = create_app(tmp_path / "pending.db", workers=0)
    with TestClient(app) as test_client:
        yield test_client


def wait_for_status(client, task_id, statuses, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/v1/tasks/{task_id}").json()["data"]
        if data["status"] in statuses:
            return data
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not reach {statuses} in time; last={data['status']}")


def create(client, **overrides):
    body = {"title": "测试任务", "task_type": "echo", "payload": {"x": 1}}
    body.update(overrides)
    resp = client.post("/api/v1/tasks", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


# --------------------------------------------------------------------------
# 创建与校验
# --------------------------------------------------------------------------

def test_create_returns_pending_task_with_envelope(client):
    resp = client.post("/api/v1/tasks", json={"title": "hello", "task_type": "echo"})
    assert resp.status_code == 201
    envelope = resp.json()
    assert envelope["error"] is None and envelope["request_id"]
    assert envelope["data"]["status"] == "pending"
    assert envelope["data"]["revision"] == 1


def test_create_rejects_unknown_task_type(client):
    resp = client.post("/api/v1/tasks", json={"title": "x", "task_type": "nope"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "UNKNOWN_TASK_TYPE"


def test_create_rejects_empty_title_and_bad_payload(client):
    assert client.post("/api/v1/tasks", json={"title": "  "}).status_code == 422
    resp = client.post("/api/v1/tasks", json={"title": "x", "payload": [1, 2]})
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# 执行生命周期
# --------------------------------------------------------------------------

def test_task_runs_to_success_with_result_and_events(client):
    task = create(client, task_type="sum_numbers", payload={"numbers": [1, 2, 3.5]})
    final = wait_for_status(client, task["id"], {"succeeded", "failed"})
    assert final["status"] == "succeeded", final.get("error")
    assert final["result"]["sum"] == 6.5
    assert final["progress"] == 100 and final["finished_at"]

    events = client.get(f"/api/v1/tasks/{task['id']}/events").json()["data"]["items"]
    kinds = [e["event_type"] for e in events]
    assert kinds[0] == "created"
    assert "status_changed" in kinds
    transitions = [(e["from_status"], e["to_status"]) for e in events if e["event_type"] == "status_changed"]
    assert ("pending", "running") in transitions and ("running", "succeeded") in transitions


def test_bad_payload_ends_failed_with_error_message(client):
    task = create(client, task_type="sum_numbers", payload={"numbers": ["not-a-number"]})
    final = wait_for_status(client, task["id"], {"failed"})
    assert final["status"] == "failed"
    assert "InvalidPayload" in final["error"]
    assert final["result"] is None


def test_simulated_failure_can_be_retried(client):
    task = create(client, task_type="simulate_work",
                  payload={"steps": 2, "step_seconds": 0.01, "fail_at": 2})
    failed = wait_for_status(client, task["id"], {"failed"})
    assert "simulated failure" in failed["error"]

    # 修复后重试：failed 任务可编辑（改掉 fail_at），再 retry 应成功
    resp = client.patch(f"/api/v1/tasks/{task['id']}",
                        json={"payload": {"steps": 2, "step_seconds": 0.01},
                              "revision": failed["revision"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["revision"] > failed["revision"]

    resp = client.post(f"/api/v1/tasks/{task['id']}/retry")
    assert resp.status_code == 200
    retried = resp.json()["data"]
    assert retried["status"] == "pending" and retried["revision"] > failed["revision"] + 1
    final = wait_for_status(client, task["id"], {"succeeded"})
    assert final["result"]["steps_done"] == 2


def test_cancel_pending_task_transitions_without_running(pending_client):
    task = create(pending_client)
    resp = pending_client.post(f"/api/v1/tasks/{task['id']}/cancel")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "cancelled" and data["finished_at"]

    events = pending_client.get(f"/api/v1/tasks/{task['id']}/events").json()["data"]["items"]
    transitions = [(e["from_status"], e["to_status"]) for e in events
                   if e["event_type"] == "status_changed"]
    assert ("pending", "cancelled") in transitions
    assert ("pending", "running") not in transitions  # 从未被执行

    remaining = pending_client.get("/api/v1/tasks", params={"status": "pending"}).json()["data"]
    assert all(item["id"] != task["id"] for item in remaining["items"])


def test_cancel_running_task_stops_cooperatively(client):
    # 10 x 0.2s = 2s 总时长：看到 running 后有约 1.8s 余量让取消落到检查点，
    # 避免任务在取消生效前就跑完（进度到 100）造成断言竞态。
    task = create(client, task_type="simulate_work",
                  payload={"steps": 10, "step_seconds": 0.2})
    wait_for_status(client, task["id"], {"running"}, timeout=5)
    assert client.post(f"/api/v1/tasks/{task['id']}/cancel").status_code == 200
    final = wait_for_status(client, task["id"], {"cancelled"}, timeout=10)
    assert final["status"] == "cancelled"
    assert final["progress"] < 100  # 未跑完就被停了


def test_delete_only_terminal_tasks(pending_client, client):
    # 非终态不可删除：pending_client 的任务永远停在 pending，断言确定性成立
    task = create(pending_client)
    resp = pending_client.delete(f"/api/v1/tasks/{task['id']}")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "INVALID_TRANSITION"

    # 终态可删除
    done = create(client)
    wait_for_status(client, done["id"], {"succeeded"})
    assert client.delete(f"/api/v1/tasks/{done['id']}").status_code == 200
    assert client.get(f"/api/v1/tasks/{done['id']}").status_code == 404


# --------------------------------------------------------------------------
# 编辑与乐观锁
# --------------------------------------------------------------------------

def test_update_pending_task_bumps_revision_and_conflict_is_detected(pending_client):
    task = create(pending_client)
    current = pending_client.get(f"/api/v1/tasks/{task['id']}").json()["data"]

    resp = pending_client.patch(f"/api/v1/tasks/{task['id']}",
                                json={"title": "改名", "revision": current["revision"]})
    assert resp.status_code == 200
    updated = resp.json()["data"]
    assert updated["title"] == "改名" and updated["revision"] == current["revision"] + 1

    stale = pending_client.patch(f"/api/v1/tasks/{task['id']}",
                                 json={"title": "再次改名", "revision": current["revision"]})
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "REVISION_CONFLICT"


def test_running_task_cannot_be_edited(client):
    task = create(client, task_type="simulate_work",
                  payload={"steps": 3, "step_seconds": 0.4})
    seen = wait_for_status(client, task["id"], {"running"}, timeout=5)
    resp = client.patch(f"/api/v1/tasks/{task['id']}", json={"title": "x", "revision": seen["revision"]})
    assert resp.status_code == 409
    wait_for_status(client, task["id"], {"succeeded"})


# --------------------------------------------------------------------------
# 查询、统计与健康检查
# --------------------------------------------------------------------------

def test_list_filter_keyword_and_stats(client):
    a = create(client, title="特殊关键词Alpha")
    create(client, title="另一个任务", task_type="word_count", payload={"text": "one two three"})
    wait_for_status(client, a["id"], {"succeeded"})

    by_keyword = client.get("/api/v1/tasks", params={"keyword": "Alpha"}).json()["data"]
    assert by_keyword["total"] == 1

    by_status = client.get("/api/v1/tasks", params={"status": "succeeded"}).json()["data"]
    assert by_status["total"] >= 1

    stats = client.get("/api/v1/stats").json()["data"]
    assert stats["by_status"]["succeeded"] >= 1
    assert any(t["task_type"] == "word_count" for t in stats["by_type"])


def test_health_and_unknown_task(client):
    assert client.get("/api/v1/health").json()["data"]["status"] == "ok"
    resp = client.get("/api/v1/tasks/99999")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "TASK_NOT_FOUND"


# --------------------------------------------------------------------------
# 分层解耦约束（防止未来把 SQL 漏进上层）
# --------------------------------------------------------------------------

def test_layers_stay_decoupled():
    import ast

    backend = BACKEND
    repo_src = (backend / "repository.py").read_text(encoding="utf-8")
    service_src = (backend / "service.py").read_text(encoding="utf-8")
    routes_src = (backend / "routes.py").read_text(encoding="utf-8")

    # repository 是唯一允许出现 SQL 关键字的地方
    for module, src in (("repository", repo_src), ("service", service_src), ("routes", routes_src)):
        has_sql = any(k in src.upper() for k in ("SELECT ", "INSERT INTO", "UPDATE tasks", "DELETE FROM"))
        if module != "repository":
            assert not has_sql, f"{module}.py 不应包含 SQL"

    # routes 不得直接依赖 repository / database
    routes_tree = ast.parse(routes_src)
    imported: set[str] = set()
    for node in ast.walk(routes_tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not imported & {"repository", "database"}, "routes 应只依赖 service/schemas"

    # service 不得直接 import sqlite3
    assert "sqlite3" not in service_src


def test_custom_handler_is_picked_up(tmp_path):
    @register("always_ten")
    def _handler(ctx: HandlerContext, payload):  # noqa: ANN001
        return {"ten": 10}

    app = create_app(tmp_path / "custom.db", workers=1)
    with TestClient(app) as c:
        task = create(c, task_type="always_ten")
        final = wait_for_status(c, task["id"], {"succeeded"})
        assert final["result"] == {"ten": 10}
