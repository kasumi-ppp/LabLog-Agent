import json
from datetime import datetime, timedelta, timezone

from lablog.chatgpt import discover_chatgpt, parse_chatgpt_event, read_snapshot, snapshot_events


def stamp(delta=timedelta(0)):
    return (datetime.now(timezone.utc) + delta).isoformat()


def snapshot(sid="chat-1", *, synced=None, turns=None, title="Synthetic chat"):
    return {
        "thread": {"id": sid, "title": title, "createdAt": stamp(timedelta(days=-1)), "updatedAt": stamp()},
        "turns": turns or [], "synced_at": synced or stamp(), "partial": True,
    }


def write_snapshot(root, sid, data):
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{sid}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_discover_reads_only_explicit_safe_ids_and_keeps_metadata_for_stale_snapshot(tmp_path):
    root = tmp_path / "bridge"
    fresh_id, stale_id = "chat-1", "chat-2"
    write_snapshot(root, fresh_id, snapshot(fresh_id))
    stale = snapshot(stale_id, synced=stamp(timedelta(hours=-30)))
    write_snapshot(root, stale_id, stale)
    # This unselected snapshot must never be included by root enumeration.
    write_snapshot(root, "unselected", snapshot("unselected"))

    result = discover_chatgpt(root, [fresh_id, stale_id, "../unselected"])
    assert result["reachable"] is False
    assert {c["id"] for c in result["conversations"]} == {fresh_id, stale_id}
    fresh = next(c for c in result["conversations"] if c["id"] == fresh_id)
    assert fresh["source"] == "chatgpt" and fresh["cwd"] == "ChatGPT"
    assert fresh["path"] == str((root / f"{fresh_id}.json").resolve())
    assert fresh["created_at"].endswith("+00:00") and fresh["updated_at"].endswith("+00:00")
    assert fresh["archived"] is False and fresh["deleted"] is False and fresh["temporary"] is False
    assert any("stale" in warning for warning in result["warnings"])
    assert any("invalid" in warning for warning in result["warnings"])


def test_discover_reports_missing_and_mismatched_id_without_exposing_outside_path(tmp_path):
    root = tmp_path / "bridge"
    write_snapshot(root, "requested", snapshot("different"))
    result = discover_chatgpt(root, ["missing", "requested"])
    assert result["reachable"] is False
    assert result["conversations"] == []
    assert len(result["warnings"]) == 2
    assert all(str(tmp_path) not in warning for warning in result["warnings"])
    try:
        read_snapshot(root / "requested.json", "requested")
    except OSError:
        pass
    else:
        raise AssertionError("mismatched thread ID must be rejected")


def test_discover_rejects_symlink_escape(tmp_path):
    root = tmp_path / "bridge"
    root.mkdir()
    outside = tmp_path / "chat-1.json"
    outside.write_text(json.dumps(snapshot()), encoding="utf-8")
    try:
        (root / "chat-1.json").symlink_to(outside)
    except (OSError, NotImplementedError):
        return
    result = discover_chatgpt(root, ["chat-1"])
    assert result["reachable"] is False and result["conversations"] == []


def test_snapshot_events_use_turn_timestamps_skip_references_and_flag_incomplete_body(tmp_path):
    sid = "chat-1"
    user_time, assistant_time = stamp(timedelta(minutes=-2)), stamp(timedelta(minutes=-1))
    data = snapshot(sid, turns=[{
        "id": "turn-1", "startedAt": user_time, "completedAt": assistant_time,
        "items": [
            {"type": "userMessage", "id": "message-user", "content": [{"type": "text", "text": "Please inspect this."}]},
            {"type": "agentMessage", "id": "message-ref", "text": "Done: ::chatgpt-content-reference omitted body"},
            {"type": "agentMessage", "id": "message-assistant", "text": "The result needs another pass."},
        ],
    }])
    write_snapshot(tmp_path / "bridge", sid, data)
    result = discover_chatgpt(tmp_path / "bridge", [sid])
    assert result["reachable"] is True
    assert any("may be incomplete" in warning for warning in result["warnings"])
    events = snapshot_events(data, sid)
    assert [event["id"] for event in events] == ["message-user", "message-assistant"]
    parsed_user, parsed_assistant = [parse_chatgpt_event(event, sid) for event in events]
    assert parsed_user["time"] == datetime.fromisoformat(user_time).astimezone(timezone.utc).isoformat()
    assert parsed_user["status"] == "planned"
    assert parsed_assistant["time"] == datetime.fromisoformat(assistant_time).astimezone(timezone.utc).isoformat()
    assert parsed_assistant["status"] == "unknown"


def test_parse_chatgpt_event_applies_done_progress_and_canonical_secret_filters():
    base = {"sessionId": "chat-1", "type": "message", "id": "m1", "time": stamp(), "role": "assistant"}
    done = parse_chatgpt_event({**base, "text": "Done: the requested work is complete."}, "chat-1")
    assert done["status"] == "completed"
    assert done["message_status"] == ""
    progress = parse_chatgpt_event({**base, "id": "m2", "text": "I am working on the request."}, "chat-1")
    assert progress["status"] == "in_progress"
    assert parse_chatgpt_event({**base, "id": "m3", "text": "No completion claim here."}, "chat-1")["status"] == "unknown"
    assert parse_chatgpt_event({**base, "id": "m4", "text": "api_key=synthetic-secret"}, "chat-1") is None
    assert parse_chatgpt_event({**base, "id": "m5", "text": "::chatgpt-content-reference"}, "chat-1") is None
    assert parse_chatgpt_event({**base, "id": "m6", "role": "system", "text": "hidden"}, "chat-1") is None
    assert parse_chatgpt_event({**base, "id": "m7", "time": "not-a-date", "text": "hello"}, "chat-1") is None
    assert parse_chatgpt_event({**base, "id": "m8", "text": "hello"}, "other-chat") is None


def test_snapshot_events_do_not_mark_open_turn_complete_even_when_text_says_done():
    sid = "chat-1"
    started = stamp()
    data = snapshot(sid, turns=[{
        "id": "turn-open", "startedAt": started, "items": [
            {"type": "agentMessage", "id": "assistant-open", "text": "Done: draft response"},
        ],
    }])
    event = snapshot_events(data, sid)[0]
    parsed = parse_chatgpt_event(event, sid)
    assert parsed["time"] == datetime.fromisoformat(started).astimezone(timezone.utc).isoformat()
    assert parsed["status"] == "in_progress"
