import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from lablog.workbuddy import discover_workbuddy, parse_workbuddy_event


def make_db(root: Path, rows=()):
    root.mkdir(parents=True, exist_ok=True)
    db = root / "workbuddy.db"
    with sqlite3.connect(db) as conn:
        conn.execute("""CREATE TABLE sessions (
            id TEXT, cwd TEXT, title TEXT, custom_title TEXT, status TEXT,
            created_at INTEGER, updated_at INTEGER, last_activity_at INTEGER,
            deleted_at INTEGER, is_playground INTEGER
        )""")
        conn.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return db


def test_discover_metadata_flags_missing_logs_and_millisecond_timestamps(tmp_path):
    root = tmp_path / "source"
    sid, deleted, temporary, absent = (
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
        "44444444-4444-4444-8444-444444444444",
    )
    rows = [
        (sid, "E:/repo", "fallback", "Custom title", "active", 1792000000123, 1792000000456, 1792000000456, None, 0),
        (deleted, "E:/repo", "Deleted", None, "closed", 1792000000000, 1792000001000, 1792000001000, 1792000002000, 0),
        (temporary, "E:/repo", "Playground", None, "active", 1792000000000, 1792000001000, 1792000001000, None, 1),
        (absent, "E:/repo", "No log", None, "active", 1792000000000, 1792000001000, 1792000001000, None, 0),
    ]
    make_db(root, rows)
    project = root / "projects" / "repo"
    project.mkdir(parents=True)
    (project / f"{sid}.jsonl").write_text("", encoding="utf-8")
    (project / f"{deleted}.jsonl").write_text("", encoding="utf-8")
    (project / f"{temporary}.jsonl").write_text("", encoding="utf-8")
    # A valid session ID in a deeper subagent directory must not be discovered.
    nested = project / "subagents"
    nested.mkdir()
    (nested / f"{absent}.jsonl").write_text("", encoding="utf-8")

    result = discover_workbuddy(root)
    assert result["reachable"] is True
    by_id = {item["id"]: item for item in result["conversations"]}
    assert set(by_id) == {sid, deleted, temporary, absent}
    assert by_id[sid]["title"] == "Custom title"
    assert by_id[sid]["source"] == "workbuddy"
    assert by_id[sid]["archived"] is False
    assert by_id[sid]["deleted"] is False
    assert by_id[sid]["temporary"] is False
    assert by_id[sid]["created_at"] == datetime.fromtimestamp(1792000000123 / 1000, timezone.utc).isoformat()
    assert by_id[deleted]["deleted"] is True
    assert by_id[temporary]["temporary"] is True
    assert by_id[absent]["path"] is None


def test_discover_is_readonly_and_reports_missing_or_malformed_db_safely(tmp_path):
    missing = discover_workbuddy(tmp_path / "missing")
    assert missing == {"conversations": [], "warnings": ["WorkBuddy database unavailable"], "reachable": False}
    root = tmp_path / "broken"
    root.mkdir()
    (root / "workbuddy.db").write_bytes(b"not a sqlite database")
    malformed = discover_workbuddy(root)
    assert malformed["reachable"] is False
    assert malformed["conversations"] == []
    assert all(str(root) not in warning for warning in malformed["warnings"])


def test_discover_rejects_log_symlink_outside_root(tmp_path):
    root = tmp_path / "source"
    sid = "11111111-1111-4111-8111-111111111111"
    make_db(root, [(sid, "E:/repo", "Title", None, "active", 1, 2, 2, None, 0)])
    project = root / "projects" / "repo"
    project.mkdir(parents=True)
    outside = tmp_path / f"{sid}.jsonl"
    outside.write_text("", encoding="utf-8")
    try:
        (project / outside.name).symlink_to(outside)
    except (OSError, NotImplementedError):
        return
    result = discover_workbuddy(root)
    assert result["conversations"][0]["path"] is None
    assert any("outside" in warning.lower() for warning in result["warnings"])


def test_parse_message_checks_session_role_timestamp_and_filters_untrusted_content():
    row = {
        "id": "event-1", "sessionId": "s1", "type": "message", "role": "assistant",
        "timestamp": 1792000000123, "status": "completed",
        "content": [
            {"type": "reasoning", "text": "DONE"},
            {"type": "output_text", "text": "The parser handles input."},
            {"type": "image", "text": "ignored"},
        ],
    }
    parsed = parse_workbuddy_event(row, "s1")
    assert parsed == {
        "event_id": "event-1", "kind": "message", "role": "assistant",
        "time": datetime.fromtimestamp(1792000000123 / 1000, timezone.utc).isoformat(),
        "text": "The parser handles input.", "status": "unknown", "message_status": "completed",
    }
    assert parse_workbuddy_event(row, "different") is None
    assert parse_workbuddy_event({**row, "role": "system"}, "s1") is None
    assert parse_workbuddy_event({**row, "timestamp": "bad"}, "s1") is None
    assert parse_workbuddy_event({**row, "id": None}, "s1") is None


def test_parse_message_status_requires_explicit_done_and_respects_incomplete():
    base = {"id": "e", "sessionId": "s", "type": "message", "role": "assistant", "timestamp": 1792000000000}
    complete = parse_workbuddy_event({**base, "status": "completed", "content": [{"type": "output_text", "text": "Done: tests passed"}]}, "s")
    assert complete["status"] == "completed"
    in_progress = parse_workbuddy_event({**base, "status": "incomplete", "content": [{"type": "output_text", "text": "Done: tests passed"}]}, "s")
    assert in_progress["status"] == "in_progress"
    ordinary = parse_workbuddy_event({**base, "status": "completed", "content": [{"type": "output_text", "text": "The tool succeeded."}]}, "s")
    assert ordinary["status"] == "unknown"
    user = parse_workbuddy_event({**base, "role": "user", "content": [{"type": "input_text", "text": "Please fix this."}]}, "s")
    assert user["status"] == "planned"


def test_parse_filters_secrets_injected_context_and_tool_arguments_and_output():
    base = {"id": "e", "sessionId": "s", "timestamp": 1792000000000}
    secret = {**base, "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "api_key=abc123"}]}
    injected = {**base, "type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>private"}]}
    assert parse_workbuddy_event(secret, "s") is None
    assert parse_workbuddy_event(injected, "s") is None
    call = {**base, "type": "function_call", "callId": "call-1", "name": "read_file", "arguments": "password=hidden"}
    parsed_call = parse_workbuddy_event(call, "s")
    assert parsed_call["kind"] == "tool" and parsed_call["role"] == "tool"
    assert parsed_call["tool_name"] == "read_file" and parsed_call["call_id"] == "call-1"
    assert "hidden" not in json.dumps(parsed_call)
    result = {**base, "id": "e2", "type": "function_call_result", "callId": "call-1", "name": "read_file", "status": "success", "output": "sensitive output"}
    parsed_result = parse_workbuddy_event(result, "s")
    assert parsed_result["text"] == "read_file: success"
    assert "sensitive output" not in json.dumps(parsed_result)
    assert parsed_result["status"] == "unknown"


def test_message_filter_rejects_quoted_json_credentials_and_bearer_tokens():
    base = {'id': 'private', 'sessionId': 's1', 'type': 'message', 'role': 'assistant', 'timestamp': 1792000000000}
    for value in ('{"access_token": "sensitive-value"}', 'Authorization: Bearer sensitive-value'):
        assert parse_workbuddy_event({**base, 'content': [{'type': 'output_text', 'text': value}]}, 's1') is None


def test_safe_non_uuid_session_ids_work_without_allowing_path_patterns(tmp_path):
    root = tmp_path / 'source'
    ids = ['s1', '../escape', '*', 'nested/session']
    rows = [(sid, 'E:/project/VR', 'Title', None, 'completed', 1792000000000, 1792000000000,
             1792000000000, None, 0) for sid in ids]
    make_db(root, rows)
    assert [c['id'] for c in discover_workbuddy(root)['conversations']] == ['s1']
