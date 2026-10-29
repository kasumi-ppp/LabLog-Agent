"""End-to-end coverage for the unified Codex and WorkBuddy conversation index."""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from lablog.conversations import ConversationIndex, EvidenceUnavailable, SelectionConflict
from lablog.sources import _allowed, _normal_path


DAY = "2026-10-13"
STAMP = "2026-10-13T01:00:00Z"
EPOCH_MS = int(datetime.fromisoformat(STAMP.replace("Z", "+00:00")).timestamp() * 1000)


def _sid(label):
    return str(uuid5(NAMESPACE_URL, f"conversation-test:{label}"))


def _wb_db(root, rows):
    root.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(root / "workbuddy.db") as conn:
        conn.execute("""CREATE TABLE sessions (
            id TEXT, cwd TEXT, title TEXT, custom_title TEXT, status TEXT,
            created_at INTEGER, updated_at INTEGER, last_activity_at INTEGER,
            deleted_at INTEGER, is_playground INTEGER
        )""")
        conn.executemany("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.close()


def _wb_row(sid, cwd, title, *, custom=None, archived=False, deleted=False):
    return (
        sid, cwd, title, custom, "closed" if archived else "active", EPOCH_MS,
        EPOCH_MS + 1000, EPOCH_MS + 1000,
        EPOCH_MS + 2000 if deleted else None, 0,
    )


def _wb_event(sid, text, *, event_id="wb-event", timestamp=EPOCH_MS, status="completed"):
    return {
        "id": event_id,
        "sessionId": sid,
        "type": "message",
        "role": "assistant",
        "status": status,
        "timestamp": timestamp,
        "content": [{"type": "output_text", "text": text}],
    }


def _write_wb(root, project_group, sid, events):
    project_dir = root / "projects" / project_group
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{sid}.jsonl"
    with path.open("a", encoding="utf-8") as stream:
        for event in events:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    return path


def _write_codex_db(root, rows):
    root.mkdir(parents=True, exist_ok=True)
    db = root / "state_5.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("""CREATE TABLE threads (
            id TEXT, cwd TEXT, title TEXT, rollout_path TEXT,
            created_at TEXT, updated_at TEXT, archived INTEGER, source TEXT
        )""")
        conn.executemany("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.close()
    return db


def _codex_row(root, session_id, *, cwd="E:/project/VR", title="Codex title", archived=False, source="vscode"):
    rollout = root / "sessions" / f"{session_id}.jsonl"
    return (session_id, cwd, title, str(rollout), STAMP, STAMP, int(archived), source)


def _write_codex(path, session_id, text, *, timestamp=STAMP, tail="", extra=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    objects = [
        {"type": "session_meta", "payload": {"id": session_id, "cwd": "E:/project/VR"}},
        {"type": "response_item", "timestamp": timestamp, "payload": {
            "type": "message", "role": "assistant", "channel": "final",
            "content": [{"type": "output_text", "text": text}],
        }},
        *extra,
    ]
    payload = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in objects) + tail
    path.write_text(payload, encoding="utf-8")


def _config(tmp_path, wbroot, codexroot):
    return {
        "_base": str(tmp_path),
        "data_dir": "data",
        "conversations": {"enabled": True},
        "workbuddy": {"enabled": True, "root": str(wbroot)},
        "codex": {
            "metadata_db": str(codexroot / "state_5.sqlite"),
            "session_roots": [str(codexroot / "sessions")],
            "project_paths": ["E:/project/VR"],
            "exclude_paths": [],
            "session_ids": [],
        },
    }


def _setup(tmp_path, *, wb_rows=(), codex_rows=()):
    wbroot = tmp_path / "wb-source"
    codexroot = tmp_path / "codex-source"
    _wb_db(wbroot, wb_rows)
    _write_codex_db(codexroot, codex_rows)
    return ConversationIndex(_config(tmp_path, wbroot, codexroot)), wbroot, codexroot


def _by_key(index, **kwargs):
    return {item["key"]: item for item in index.list(**kwargs)["items"]}


def _chatgpt_setup(tmp_path):
    index, _, _ = _setup(tmp_path)
    root = tmp_path / 'cloud-bridge'
    root.mkdir()
    index.cfg['chatgpt'] = {'enabled': True, 'root': str(root), 'session_ids': ['cloud-one'], 'max_age_hours': 24}
    payload = {'thread': {'id': 'cloud-one', 'title': '论文分析', 'createdAt': EPOCH_MS / 1000, 'updatedAt': EPOCH_MS / 1000},
               'synced_at': datetime.now(timezone.utc).isoformat(), 'partial': True,
               'turns': [{'id': 'turn-one', 'startedAt': EPOCH_MS / 1000, 'completedAt': EPOCH_MS / 1000 + 10,
                          'items': [{'type': 'userMessage', 'id': 'user-one', 'content': [{'type': 'text', 'text': '请分析论文重点'}]},
                                    {'type': 'agentMessage', 'id': 'agent-one', 'text': '::chatgpt-content-reference{index="0"}'}]}]}
    path = root / 'cloud-one.json'
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    return index, path, payload


def test_chatgpt_monitor_uses_authorized_messages_dates_and_durable_selection(tmp_path):
    index, path, payload = _chatgpt_setup(tmp_path)
    result = index.collect('daily', DAY)
    assert result['blockers'] == []
    assert len(result['facts']) == 1
    fact = result['facts'][0]
    assert fact['source'] == 'chatgpt' and fact['status'] == 'planned'
    assert fact['text'] == '请分析论文重点'
    row = index.list(source='chatgpt')['items'][0]
    assert row['title'] == '论文分析' and row['selected']
    assert index.projects()[0]['counts']['chatgpt'] == 1
    assert index.evidence(row['key'], fact['message_id'])['text'] == fact['text']
    assert index.messages(row['key'], 'daily', DAY)['total'] == 1
    assert index.messages(row['key'], 'daily', '2026-10-14')['total'] == 0
    assert index.messages(row['key'], 'daily', DAY, view='tools')['tools'] == []
    index.select([{'key': row['key'], 'revision': row['revision'], 'selected': False}])
    restarted = ConversationIndex(index.cfg)
    assert restarted.collect('daily', DAY)['facts'] == []
    payload['thread']['title'] = '重命名论文分析'
    path.write_text(json.dumps(payload), encoding='utf-8')
    restarted.refresh()
    assert restarted.list(source='chatgpt')['items'][0]['selected'] is False


def test_chatgpt_stale_snapshot_blocks_and_disabled_or_unlisted_cannot_be_opted_in(tmp_path):
    index, path, payload = _chatgpt_setup(tmp_path)
    index.refresh()
    row = index.list(source='chatgpt')['items'][0]
    payload['synced_at'] = '2000-01-01T00:00:00+00:00'
    path.write_text(json.dumps(payload), encoding='utf-8')
    result = index.collect('daily', DAY)
    assert result['blockers']
    assert index.list(source='chatgpt')['items'][0]['message_count'] == 1
    index.cfg['chatgpt']['enabled'] = False
    assert index.collect('daily', DAY)['facts'] == []
    assert not index.list(source='chatgpt')['items'][0]['available']
    with pytest.raises(EvidenceUnavailable):
        index.select([{'key': row['key'], 'revision': row['revision'], 'selected': True}])
    index.cfg['chatgpt']['enabled'] = True
    index.cfg['chatgpt']['session_ids'] = []
    assert index.collect('daily', DAY)['facts'] == []
    with pytest.raises(EvidenceUnavailable):
        index.select([{'key': row['key'], 'revision': row['revision'], 'selected': True}])


def test_removing_optional_chatgpt_config_keeps_old_directory_disabled(tmp_path):
    index, _, _ = _chatgpt_setup(tmp_path)
    index.refresh()
    del index.cfg['chatgpt']
    assert index.collect('daily', DAY)['facts'] == []
    assert index.projects()[0]['counts']['chatgpt'] == 1
    assert index.list(source='chatgpt')['items'][0]['available'] is False
    assert next(s for s in index.sources() if s['id'] == 'chatgpt')['reachable'] is None


def test_extended_windows_paths_share_project_selection_and_exclusions(tmp_path):
    assert _normal_path(r"\\?\E:\project\VR") == _normal_path("E:/project/VR")
    assert _normal_path(r"\\?\UNC\server\share\VR") == "//server/share/vr"
    assert not _allowed(r"\\?\E:\project\VR\private", ["E:/project/VR"], ["E:/project/VR/private"])
    sid = _sid("extended-path")
    codexroot = tmp_path / "codex-source"
    index, wbroot, codexroot = _setup(tmp_path, wb_rows=[_wb_row(sid, "E:/project/VR", "WB")],
        codex_rows=[_codex_row(codexroot, "extended", cwd=r"\\?\E:\project\VR\src")])
    _write_wb(wbroot, "vr", sid, [_wb_event(sid, "正在完善界面")])
    _write_codex(codexroot / "sessions/extended.jsonl", "extended", "正在完善布局")
    index.refresh()
    rows = _by_key(index)
    assert rows["codex:extended"]["selected"] is True
    assert rows["codex:extended"]["project_id"] == rows[f"workbuddy:{sid}"]["project_id"]
    assert index.projects()[0]["counts"] == {"codex": 1, "workbuddy": 1}
    assert {fact["source"] for fact in index.collect("daily", DAY)["facts"]} == {"codex", "workbuddy"}


@pytest.mark.skipif(os.name != "nt", reason="Native Windows extended path access")
def test_extended_windows_rollout_paths_remain_inside_source_root(tmp_path):
    codexroot = tmp_path / "codex-source"
    row = list(_codex_row(codexroot, "extended-log"))
    row[3] = "\\\\?\\" + str(Path(row[3]).resolve())
    index, _, _ = _setup(tmp_path, codex_rows=[row])
    _write_codex(Path(row[3]), "extended-log", "正在验证原始证据")
    result = index.collect("daily", DAY)
    assert not result["blockers"]
    assert len(result["facts"]) == 1
    assert index.messages("codex:extended-log", "daily", DAY)["total"] == 1


def test_compressed_codex_archive_keeps_original_message_locators(tmp_path):
    import zstandard
    codexroot = tmp_path / "codex-source"
    row = _codex_row(codexroot, "compressed", archived=True)
    index, _, _ = _setup(tmp_path, codex_rows=[row])
    log = Path(row[3])
    _write_codex(log, "compressed", "已完成归档证据测试，待独立验证。")
    compressed = log.with_suffix(".jsonl.zst")
    compressed.write_bytes(zstandard.ZstdCompressor().compress(log.read_bytes()))
    log.unlink()
    result = index.collect("daily", DAY)
    assert result["blockers"] == []
    assert len(result["facts"]) == 1
    fact = result["facts"][0]
    assert index.evidence("codex:compressed", fact["message_id"])["text"] == fact["text"]
    assert index.messages("codex:compressed", "daily", DAY)["total"] == 1
    assert index.list(include_archived=True)["items"][0]["message_count"] == 1
    compressed.write_bytes(b"corrupted archive")
    assert index.collect("daily", DAY)["blockers"]
    # A failed replacement must retain the cached historical summary.
    assert index.list(include_archived=True)["items"][0]["message_count"] == 1


def test_refresh_groups_all_workbuddy_metadata_and_applies_selection_defaults(tmp_path):
    outside_id, inside_id, deleted_id = _sid("outside"), _sid("inside"), _sid("deleted")
    codexroot = tmp_path / "codex-source"
    archived_codex = "archived-codex"
    index, wbroot, codexroot = _setup(tmp_path, wb_rows=[
        _wb_row(outside_id, "E:/project/Other", "Raw", custom="Renamed outside"),
        _wb_row(inside_id, "E:/project/VR", "VR conversation"),
        _wb_row(deleted_id, "E:/project/Other", "Deleted", deleted=True),
    ], codex_rows=[_codex_row(codexroot, archived_codex, title="Archived", archived=True)])
    _write_wb(wbroot, "other", outside_id, [_wb_event(outside_id, "Finished the outside project")])
    _write_wb(wbroot, "vr", inside_id, [_wb_event(inside_id, "Finished the VR project")])
    _write_wb(wbroot, "other", deleted_id, [_wb_event(deleted_id, "Deleted work")])
    _write_codex(codexroot / "sessions" / f"{archived_codex}.jsonl", archived_codex, "Archived work")

    refreshed = index.refresh()
    assert set(refreshed) == {"sources", "warnings"}
    assert {source["id"] for source in refreshed["sources"]} >= {"codex", "workbuddy"}
    assert isinstance(refreshed["warnings"], list)
    projects = index.projects()
    assert all({"id", "name", "conversation_count", "counts"} <= p.keys() for p in projects)
    assert all({"codex", "workbuddy"} <= p["counts"].keys() for p in projects)

    visible = _by_key(index)
    assert f"workbuddy:{outside_id}" in visible  # Manual opt-in targets outside VR too.
    assert visible[f"workbuddy:{outside_id}"]["title"] == "Renamed outside"
    assert visible[f"workbuddy:{outside_id}"]["selected"] is False
    assert visible[f"workbuddy:{inside_id}"]["selected"] is True
    assert f"codex:{archived_codex}" not in visible
    assert f"workbuddy:{deleted_id}" not in visible
    with_deleted = _by_key(index, include_deleted=True)
    assert with_deleted[f"workbuddy:{deleted_id}"]["deleted"] is True
    assert index.list(include_archived=True)["total"] >= 3


def test_selection_survives_title_refresh_and_new_index_instance(tmp_path):
    sid = _sid("manual-session")
    index, wbroot, codexroot = _setup(tmp_path, wb_rows=[_wb_row(sid, "E:/project/Other", "Original")])
    _write_wb(wbroot, "other", sid, [_wb_event(sid, "Manual evidence")])
    index.refresh()
    row = _by_key(index)[f"workbuddy:{sid}"]
    updated = index.select([{"key": row["key"], "selected": True, "revision": row["revision"]}])[0]
    assert updated["selected"] is True

    # A metadata title update must preserve the selection stored in the app cache.
    with sqlite3.connect(wbroot / "workbuddy.db") as conn:
        conn.execute("UPDATE sessions SET custom_title=? WHERE id=?", ("A newer title", sid))
    reloaded = ConversationIndex(_config(tmp_path, wbroot, codexroot))
    reloaded.refresh()
    after = _by_key(reloaded)[f"workbuddy:{sid}"]
    assert after["title"] == "A newer title"
    assert after["selected"] is True

    canceled = reloaded.select([{"key": after["key"], "selected": False, "revision": after["revision"]}])[0]
    reopened = ConversationIndex(_config(tmp_path, wbroot, codexroot))
    reopened.refresh()
    assert _by_key(reopened)[canceled["key"]]["selected"] is False


def test_same_native_id_from_independent_sources_stays_distinct_and_subagents_are_excluded(tmp_path):
    same_id = _sid("shared-native-id")
    sid = "main-thread"
    index, wbroot, codexroot = _setup(
        tmp_path,
        wb_rows=[_wb_row(same_id, "E:/project/VR", "WB title")],
        codex_rows=[_codex_row(codexroot := tmp_path / "codex-source", same_id, title="Codex title")],
    )
    # The same native ID is intentionally represented in both source stores.
    _write_wb(wbroot, "vr", same_id, [_wb_event(same_id, "WorkBuddy evidence")])
    codex_session = codexroot / "sessions" / f"{same_id}.jsonl"
    _write_codex(codex_session, same_id, "Codex evidence")
    index.refresh()
    keys = _by_key(index)
    assert keys[f"codex:{same_id}"]["title"] == "Codex title"
    assert keys[f"workbuddy:{same_id}"]["title"] == "WB title"

    # Metadata rows marked as subagents must not be surfaced as conversations.
    agent_row = _codex_row(codexroot, sid, source=json.dumps({"kind": "subagent"}))
    with sqlite3.connect(codexroot / "state_5.sqlite") as conn:
        conn.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?)", agent_row)
    agent_session = codexroot / "sessions" / f"{sid}.jsonl"
    _write_codex(agent_session, sid, "Subagent evidence")
    index.refresh()
    assert f"codex:{sid}" not in _by_key(index)


def test_archive_and_delete_filters_never_collect_deleted_content(tmp_path):
    archived, deleted, active = "archived-codex", _sid("deleted"), _sid("active")
    codexroot = tmp_path / "codex-source"
    index, wbroot, codexroot = _setup(tmp_path, wb_rows=[
        _wb_row(deleted, "E:/project/VR", "Deleted", deleted=True),
        _wb_row(active, "E:/project/VR", "Active"),
    ], codex_rows=[_codex_row(codexroot, archived, title="Archived", archived=True)])
    _write_codex(codexroot / "sessions" / f"{archived}.jsonl", archived, "Archived evidence")
    _write_wb(wbroot, "vr", deleted, [_wb_event(deleted, "DELETED_SECRET_EVIDENCE")])
    _write_wb(wbroot, "vr", active, [_wb_event(active, "Active evidence")])
    index.refresh()
    assert index.list()["total"] == 1
    assert index.list(include_archived=True)["total"] == 2
    assert index.list(include_deleted=True)["total"] == 2
    assert "DELETED_SECRET_EVIDENCE" not in json.dumps(index.collect("daily", DAY), ensure_ascii=False)
    with pytest.raises(EvidenceUnavailable):
        index.messages(f"workbuddy:{deleted}", "daily", DAY)


def test_message_day_selection_and_selected_only_collection_across_sources(tmp_path):
    outside, inside, codex_id = _sid("outside"), _sid("inside"), "codex-vr"
    codexroot = tmp_path / "codex-source"
    index, wbroot, _ = _setup(
        tmp_path,
        wb_rows=[_wb_row(outside, "E:/project/Elsewhere", "Outside"), _wb_row(inside, "E:/project/VR", "Inside")],
        codex_rows=[_codex_row(codexroot, codex_id)],
    )
    _write_wb(wbroot, "elsewhere", outside, [_wb_event(outside, "Outside manual selection")])
    _write_wb(wbroot, "vr", inside, [_wb_event(inside, "VR deselected")])
    _write_codex(codexroot / "sessions" / f"{codex_id}.jsonl", codex_id, "Codex selected work")
    index.refresh()
    rows = _by_key(index)
    index.select([
        {"key": f"workbuddy:{outside}", "selected": True, "revision": rows[f"workbuddy:{outside}"]["revision"]},
        {"key": f"workbuddy:{inside}", "selected": False, "revision": rows[f"workbuddy:{inside}"]["revision"]},
    ])

    collected = index.collect("daily", DAY)
    texts = [fact["text"] for fact in collected["facts"]]
    assert any("Outside manual selection" in text for text in texts)
    assert any("Codex selected work" in text for text in texts)
    assert not any("VR deselected" in text for text in texts)

    messages = index.messages(f"workbuddy:{outside}", "daily", DAY)
    assert any("Outside manual selection" in item.get("text", "") for item in messages["messages"])
    next_day = index.messages(f"workbuddy:{outside}", "daily", "2026-10-14")
    assert next_day["messages"] == []


def test_incremental_refresh_handles_append_partial_tail_completion_and_rewrite(tmp_path):
    sid = _sid("incremental")
    index, wbroot, _ = _setup(tmp_path, wb_rows=[_wb_row(sid, "E:/project/VR", "Incremental")])
    log = _write_wb(wbroot, "vr", sid, [_wb_event(sid, "First message", event_id="one")])
    index.refresh()
    assert index.list()["items"][0]["message_count"] == 1
    assert len(index.collect("daily", DAY)["facts"]) == 1

    complete_line = json.dumps(_wb_event(sid, "Third message", event_id="three"), ensure_ascii=False) + "\n"
    split = len(complete_line) // 2
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(_wb_event(sid, "Second message", event_id="two"), ensure_ascii=False) + "\n")
        stream.write(complete_line[:split])
    index.refresh()
    assert index.list()["items"][0]["message_count"] == 2
    result = index.collect("daily", DAY)
    assert len(result["facts"]) == 2
    assert any("partial" in warning.lower() for warning in result["warnings"])

    with log.open("a", encoding="utf-8") as stream:
        stream.write(complete_line[split:])
    index.refresh()
    assert index.list()["items"][0]["message_count"] == 3
    assert len(index.collect("daily", DAY)["facts"]) == 3

    # A same-path truncate/rewrite invalidates byte-offset state; old facts vanish.
    log.write_text(json.dumps(_wb_event(sid, "Replacement message", event_id="replacement"), ensure_ascii=False) + "\n", encoding="utf-8")
    index.refresh()
    final = index.collect("daily", DAY)
    assert len(final["facts"]) == 1
    assert "Replacement message" in final["facts"][0]["text"]
    assert index.list()["items"][0]["message_count"] == 1


def test_codex_incremental_cache_does_not_store_full_long_raw_message(tmp_path):
    sid = "long-codex"
    codexroot = tmp_path / "codex-source"
    index, _, _ = _setup(tmp_path, codex_rows=[_codex_row(codexroot, sid)])
    long_text = "UNIQUE_LONG_BODY_" + ("x" * 2000)
    path = codexroot / "sessions" / f"{sid}.jsonl"
    _write_codex(path, sid, long_text)
    index.refresh()
    shown = index.messages(f"codex:{sid}", "daily", DAY)
    assert any(long_text in item.get("text", "") for item in shown["messages"])

    cache_dbs = list((tmp_path / "data").rglob("*.sqlite")) + list((tmp_path / "data").rglob("*.db"))
    assert cache_dbs, "conversation index should persist its incremental cache under data_dir"
    stored_strings = []
    for db in cache_dbs:
        with sqlite3.connect(db) as conn:
            tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                escaped = table.replace('"', '""')
                for row in conn.execute(f'SELECT * FROM "{escaped}"'):
                    stored_strings.extend(value for value in row if isinstance(value, str))
    assert long_text not in stored_strings
    assert not any("x" * 1000 in value for value in stored_strings)


def test_codex_repeated_text_across_days_keeps_independent_messages(tmp_path):
    codexroot = tmp_path / "codex-source"
    index, _, _ = _setup(tmp_path, codex_rows=[_codex_row(codexroot, "repeat")])
    log = codexroot / "sessions/repeat.jsonl"
    _write_codex(log, "repeat", "正在执行回归测试")
    event = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    event["timestamp"] = "2026-10-14T01:00:00Z"
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")  # Duplicate event is still counted once.
    index.refresh()
    assert index.list()["items"][0]["message_count"] == 2
    assert len(index.collect("daily", DAY)["facts"]) == 1
    assert len(index.collect("daily", "2026-10-14")["facts"]) == 1


def test_short_log_append_only_parses_new_events(tmp_path, monkeypatch):
    sid = _sid("short-append")
    index, wbroot, _ = _setup(tmp_path, wb_rows=[_wb_row(sid, "E:/project/VR", "Short append")])
    log = _write_wb(wbroot, "vr", sid, [_wb_event(sid, "First", event_id="first")])
    index.refresh()
    parsed = []
    original = index._parse
    def capture(row, item):
        parsed.append(row["id"])
        return original(row, item)
    monkeypatch.setattr(index, "_parse", capture)
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(_wb_event(sid, "Second", event_id="second")) + "\n")
    index.refresh()
    assert parsed == ["second"]
    assert index.list()["items"][0]["message_count"] == 2


def test_message_paging_reports_counts_and_tools_separately(tmp_path):
    sid = _sid("paging")
    index, wbroot, _ = _setup(tmp_path, wb_rows=[_wb_row(sid, "E:/project/VR", "Paging")])
    events = [_wb_event(sid, f"Message {n}", event_id=f"e{n}") for n in range(3)]
    _write_wb(wbroot, "vr", sid, events)
    index.refresh()
    first = index.messages(f"workbuddy:{sid}", "daily", DAY, offset=0, limit=2)
    second = index.messages(f"workbuddy:{sid}", "daily", DAY, offset=2, limit=2)
    assert len(first["messages"]) == 2
    assert first["total"] == second["total"] == 3
    assert first["next_offset"] == 2
    assert second["next_offset"] is None
    assert isinstance(first["tools"], list)


def test_selection_conflict_is_stale_and_atomic_for_batch(tmp_path):
    one, two = _sid("one"), _sid("two")
    index, wbroot, _ = _setup(tmp_path, wb_rows=[
        _wb_row(one, "E:/project/VR", "One"), _wb_row(two, "E:/project/VR", "Two")])
    _write_wb(wbroot, "vr", one, [_wb_event(one, "One")])
    _write_wb(wbroot, "vr", two, [_wb_event(two, "Two")])
    index.refresh()
    rows = _by_key(index)
    old_revision = rows[f"workbuddy:{one}"]["revision"]
    index.select([{"key": f"workbuddy:{one}", "selected": False, "revision": old_revision}])
    before = _by_key(index)
    with pytest.raises(SelectionConflict):
        index.select([
            {"key": f"workbuddy:{two}", "selected": False, "revision": before[f"workbuddy:{two}"]["revision"]},
            {"key": f"workbuddy:{one}", "selected": True, "revision": old_revision},
        ])
    after = _by_key(index)
    assert after[f"workbuddy:{two}"]["selected"] is True
    assert after[f"workbuddy:{one}"]["selected"] is False


def test_disabled_source_and_explicit_exclusion_override_saved_selection(tmp_path):
    sid = _sid('disabled')
    index, wbroot, _ = _setup(tmp_path, wb_rows=[_wb_row(sid, 'E:/project/Other', 'Optional work')])
    _write_wb(wbroot, 'other', sid, [_wb_event(sid, '已完成本地验证。')])
    index.refresh()
    row = _by_key(index)[f'workbuddy:{sid}']
    index.select([{'key': row['key'], 'selected': True, 'revision': row['revision']}])
    index.cfg['workbuddy']['enabled'] = False
    assert index.collect('daily', DAY)['facts'] == []
    disabled = _by_key(index)[row['key']]
    assert disabled['available'] is False
    with pytest.raises(EvidenceUnavailable):
        index.select([{'key': row['key'], 'selected': True, 'revision': disabled['revision']}])
    assert _by_key(index)[row['key']]['revision'] == disabled['revision']
    index.cfg['workbuddy']['enabled'] = True
    index.cfg['workbuddy']['exclude_paths'] = ['E:/project/Other']
    assert index.collect('daily', DAY)['facts'] == []


def test_native_source_failure_retains_directory_but_blocks_current_collection(tmp_path):
    sid = _sid('lost-source')
    index, wbroot, _ = _setup(tmp_path, wb_rows=[_wb_row(sid, 'E:/project/VR', 'Work')])
    _write_wb(wbroot, 'vr', sid, [_wb_event(sid, '已完成本地验证。')])
    index.refresh()
    (wbroot / 'workbuddy.db').unlink()
    result = index.collect('daily', DAY)
    assert result['facts'] == []
    assert result['blockers']
    assert _by_key(index)[f'workbuddy:{sid}']['title'] == 'Work'


def test_removed_native_row_is_retained_as_deleted_directory_entry(tmp_path):
    sid = _sid('removed-row')
    index, wbroot, _ = _setup(tmp_path, wb_rows=[_wb_row(sid, 'E:/project/VR', 'Removed')])
    _write_wb(wbroot, 'vr', sid, [_wb_event(sid, '已完成待验证工作。')])
    index.refresh()
    with sqlite3.connect(wbroot / 'workbuddy.db') as conn:
        conn.execute('DELETE FROM sessions WHERE id=?', (sid,))
    conn.close()
    index.refresh()
    assert index.list()['total'] == 0
    removed = index.list(include_deleted=True)['items'][0]
    assert removed['deleted'] and not removed['selected'] and not removed['available']
    assert index.projects()[0]['conversation_count'] == 0
    assert index.collect('daily', DAY)['facts'] == []


def test_replaced_log_with_same_prefix_rebuilds_old_event_locators(tmp_path):
    sid = _sid('replace-grown')
    index, wbroot, _ = _setup(tmp_path, wb_rows=[_wb_row(sid, 'E:/project/VR', 'Replacement')])
    events = [_wb_event(sid, '原记录' + '甲' * 300, event_id=str(n)) for n in range(20)]
    log = _write_wb(wbroot, 'vr', sid, events)
    index.refresh()
    events[-1] = _wb_event(sid, '更新后的记录' + '乙' * 300, event_id='19')
    events.append(_wb_event(sid, '追加记录' + '丙' * 500, event_id='20'))
    replacement = log.with_suffix('.replacement')
    replacement.write_text(''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in events), encoding='utf-8')
    replacement.replace(log)
    page = index.messages(f'workbuddy:{sid}', 'daily', DAY)
    assert page['total'] == 21
    assert page['messages'][19]['text'].startswith('更新后的记录')


def test_explicit_run_scope_cannot_be_bypassed_by_saved_codex_choice(tmp_path):
    from lablog.cli import _apply_scope
    sid = 'outside-codex'
    cx = tmp_path / 'codex-source'
    index, _, cx = _setup(tmp_path, codex_rows=[_codex_row(cx, sid, cwd='E:/project/Other')])
    _write_codex(cx / 'sessions' / f'{sid}.jsonl', sid, '已完成外部项目工作。')
    index.refresh()
    row = _by_key(index)[f'codex:{sid}']
    index.select([{'key': row['key'], 'selected': True, 'revision': row['revision']}])
    assert index.collect('daily', DAY)['facts']
    scope = tmp_path / 'run-scope.json'
    scope.write_text(json.dumps({'project_paths': ['E:/project/VR'], 'session_ids': []}), encoding='utf-8')
    restricted = ConversationIndex(_apply_scope(index.cfg, scope))
    assert restricted.collect('daily', DAY)['facts'] == []
