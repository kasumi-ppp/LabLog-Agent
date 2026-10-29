import json
from datetime import date

import pytest

from lablog import weekly_pool


def cfg(tmp_path):
    return {"_base": str(tmp_path), "data_dir": "data"}


def fact(text, day, ident="e", **kwargs):
    return {"text": text, "date": day, "status": "completed", "role": "assistant",
            "source": "codex", "evidence_id": ident, "session_id": "s", "project": "p", **kwargs}


def test_saturday_sunday_dates_and_timezone_fallback(tmp_path):
    c = cfg(tmp_path)
    weekly_pool.sync(c, "2026-10-16", [fact("Saturday work", "2026-10-17", "sat"),
                                           {"text": "Sunday work", "time": "2026-10-18T15:30:00Z",
                                            "status": "committed", "source": "git", "evidence_id": "sun"}])
    items = weekly_pool.load(c)["items"]
    assert {i["work_date"] for i in items} == {"2026-10-17", "2026-10-18"}
    assert all(i["week_start"] == "2026-10-12" for i in items)


def test_content_dedup_unions_evidence_and_provenance_and_current_week_first(tmp_path):
    c = cfg(tmp_path)
    weekly_pool.sync(c, "2026-10-16", [fact("  Fix   parser ", "2026-10-11", "old"),
        fact("fix parser", "2026-10-12", "new", source="git", message_id="m"),
        fact("current week", "2026-10-14", "now")])
    items = weekly_pool.load(c)["items"]
    assert len(items) == 2
    same = next(i for i in items if "parser" in i["text"].lower())
    assert same["work_date"] == "2026-10-11"
    assert same["evidence_ids"] == ["old", "new"]
    assert len(same["provenance"]) == 2
    selected = weekly_pool.select(c, "2026-10-16", 1)
    assert selected[0]["text"] == "current week"


def test_selection_excludes_and_returns_whole_items_to_target(tmp_path):
    c = cfg(tmp_path)
    weekly_pool.sync(c, "2026-10-16", [fact("one", "2026-10-12", "a"),
                                           fact("two", "2026-10-13", "b"),
                                           fact("three", "2026-10-14", "c")])
    chosen = weekly_pool.select(c, "2026-10-16", 7, exclude_evidence_ids=["a"], exclude_texts=["three"])
    assert [x["text"] for x in chosen] == ["two"]
    assert weekly_pool.select(c, "2026-10-16", 0) == []


def test_selection_retry_does_not_consume_and_report_receipt_is_idempotent(tmp_path):
    c = cfg(tmp_path)
    weekly_pool.sync(c, "2026-10-16", [fact("work", "2026-10-12")])
    picked = weekly_pool.select(c, "2026-10-16", 200)
    assert weekly_pool.select(c, "2026-10-16", 200) == picked
    item_id = picked[0]["id"]
    assert weekly_pool.mark_reported(c, "2026-10-16", [item_id], "r1") == 1
    assert weekly_pool.mark_reported(c, "2026-10-16", [item_id], "r1") == 0
    assert weekly_pool.select(c, "2026-10-19", 1) == []
    # Rediscovery in a later fresh window never resurrects consumed work.
    weekly_pool.sync(c, "2026-10-19", [fact("work", "2026-10-12")])
    assert weekly_pool.select(c, "2026-10-19", 1) == []


def test_expiry_on_monday_and_raw_logs_are_untouched(tmp_path):
    c = cfg(tmp_path)
    raw = tmp_path / "data" / "raw.json"
    raw.parent.mkdir(parents=True)
    raw.write_text('{"keep": true}', encoding="utf-8")
    weekly_pool.sync(c, "2026-10-16", [fact("Oct 12 work", "2026-10-12", "a"),
                                           fact("Oct 5 work", "2026-10-05", "b")])
    weekly_pool.sync(c, "2026-10-26", [fact("Oct 12 work", "2026-10-12", "a")])
    assert weekly_pool.load(c)["items"] == []
    assert json.loads(raw.read_text(encoding="utf-8")) == {"keep": True}


def test_unselected_facts_removed_and_user_or_planned_facts_excluded(tmp_path):
    c = cfg(tmp_path)
    weekly_pool.sync(c, "2026-10-16", [fact("done", "2026-10-14"),
        fact("user assertion", "2026-10-14", "u", role="user"),
        fact("planned", "2026-10-14", "p", status="planned")])
    assert [i["text"] for i in weekly_pool.load(c)["items"]] == ["done"]
    weekly_pool.sync(c, "2026-10-16", [])
    assert weekly_pool.load(c)["items"] == []


def test_malformed_pool_is_reported_without_repair_or_erasure(tmp_path):
    c = cfg(tmp_path)
    path = tmp_path / "data" / "weekly-pool" / "items.json"
    path.parent.mkdir(parents=True)
    path.write_text('{broken', encoding="utf-8")
    with pytest.raises(ValueError):
        weekly_pool.load(c)
    assert path.read_text(encoding="utf-8") == '{broken'


def test_original_expiry_applies_after_rediscovery_and_during_select(tmp_path):
    c = cfg(tmp_path)
    original = fact("same work repeated", "2026-10-12")
    weekly_pool.sync(c, "2026-10-16", [original])
    # Selection enforces the expiry even if nobody has synchronized since then.
    assert weekly_pool.select(c, "2026-10-26", 1) == []
    repeated = fact("same work repeated", "2026-10-26", "later")
    weekly_pool.sync(c, "2026-10-26", [repeated])
    assert weekly_pool.load(c)["items"] == []


def test_verified_receipt_survives_removed_item_and_merges_same_report_ids(tmp_path):
    c = cfg(tmp_path)
    weekly_pool.sync(c, "2026-10-16", [fact("one", "2026-10-12", "a"),
                                           fact("two", "2026-10-13", "b")])
    ids = [item["id"] for item in weekly_pool.load(c)["items"]]
    weekly_pool.sync(c, "2026-10-16", [])
    assert weekly_pool.mark_reported(c, "2026-10-16", ids[:1], "verified-report") == 1
    assert weekly_pool.mark_reported(c, "2026-10-16", ids, "verified-report") == 1
    receipt = weekly_pool.load(c)["receipts"][0]
    assert set(receipt["item_ids"]) == set(ids)
    assert weekly_pool.mark_reported(c, "2026-10-19", ids[:1], "next-report") == 0


def test_selection_uses_meaningful_character_count_and_provenance_ids(tmp_path):
    c = cfg(tmp_path)
    source = fact("a       b", "2026-10-12", "primary", source_path="notes.md", source_line=4,
                  supporting_evidence=[{"evidence_id": "other", "source": "git",
                                        "source_path": "repo", "source_line": 9}])
    weekly_pool.sync(c, "2026-10-16", [source, fact("xyz", "2026-10-13", "second")])
    item = weekly_pool.load(c)["items"][0]
    assert item["evidence_ids"] == ["primary", "other"]
    assert any(p.get("source_path") == "notes.md" and p.get("source_line") == 4
               for p in item["provenance"])
    assert any(p.get("source_path") == "repo" and p.get("source_line") == 9
               for p in item["provenance"])
    # Internal whitespace does not falsely reach the five meaningful character target.
    assert [x["id"] for x in weekly_pool.select(c, "2026-10-16", 5)] == [
        item["id"], weekly_pool.load(c)["items"][1]["id"]]
