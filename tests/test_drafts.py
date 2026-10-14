"""Draft overwrite protection and evidence retention (P1)."""

import json
import os
from datetime import date
from pathlib import Path

import pytest

from lablog import cli as cli_module
from lablog.workflow import SubmissionBlocked, atomic_json

DAY = "2026-10-14"


def _template():
    return {"report_template_id": "t", "report_template_name": "学生日报", "allowAddReceivers": 1,
            "lastModifiedTime": 1, "report_template_fields": [
                {"field_name": "今日完成", "field_sort": 0, "field_type": 1},
                {"field_name": "心得体会", "field_sort": 1, "field_type": 1}]}


class FakeTemplateDws:
    def auth(self):
        return {"corp_id": "corp", "user_id": "self", "user_name": "Self"}

    def template(self, name, ident):
        return _template()

    def contact(self, ident):
        return {"orgUserName": "Recipient", "orgName": "lab"}

    def outbox(self, name, start, end):
        return []


def gen_cfg(tmp_path, monkeypatch, *, notes=None, drop_notes=False):
    work = tmp_path / "work"
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "s.jsonl").write_text(json.dumps({"type": "session_meta", "payload": {"id": "s", "cwd": str(work)}})
                                      + "\n" + json.dumps({"type": "response_item", "timestamp": "2026-10-14T01:00:00Z",
                                                           "payload": {"type": "message", "role": "assistant",
                                                                       "channel": "final",
                                                                       "content": [{"type": "output_text",
                                                                                    "text": "Completed parser"}]}}),
                                      encoding="utf-8")
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    if notes:
        (notes_dir / f"{DAY}.md").write_text(notes, encoding="utf-8")
    if drop_notes:
        for child in list(notes_dir.iterdir()):
            child.unlink()
        notes_dir.rmdir()
    cfg = {"_base": str(tmp_path), "data_dir": "data",
           "codex": {"mode": "selected", "session_roots": [str(sessions)], "project_paths": [str(work)]},
           "supplemental": {"markdown_paths": [str(notes_dir)]},
           "templates": {"daily": {"id": "t", "name": "学生日报"}}, "quality": {}}
    monkeypatch.setattr(cli_module, "_client", lambda _: FakeTemplateDws())
    return cfg


def _draft_bytes(path):
    return Path(path).read_bytes()


# --------------------------------------------------------------------------
# P1-9 draft overwrite protection
# --------------------------------------------------------------------------

def test_repeated_generation_is_a_no_op(tmp_path, monkeypatch):
    cfg = gen_cfg(tmp_path, monkeypatch, notes="修复解析器\n")
    path, first = cli_module._generate(cfg, "daily", DAY, "extractive")
    before = _draft_bytes(path)
    path2, second, info = cli_module._generate_impl(cfg, "daily", DAY, "extractive")
    assert info["action"] == "unchanged", "an unchanged draft must not be rewritten"
    assert second["hash"] == first["hash"]
    assert _draft_bytes(path2) == before


def test_edited_draft_is_snapshotted_before_replacing(tmp_path, monkeypatch):
    cfg = gen_cfg(tmp_path, monkeypatch, notes="修复解析器\n")
    path, first = cli_module._generate(cfg, "daily", DAY, "extractive")
    current = json.loads(path.read_text(encoding="utf-8"))
    current["fields"][0]["value"] = "我手工改过的正文"
    atomic_json(path, current)
    path2, second, info = cli_module._generate_impl(cfg, "daily", DAY, "extractive")
    assert info["action"] == "snapshot" and info["archive"]
    archive = Path(info["archive"])
    assert archive.is_file()
    assert json.loads(archive.read_text(encoding="utf-8"))["fields"][0]["value"] == "我手工改过的正文"
    values = [f["value"] for f in second["fields"] if f["name"] == "今日完成"]
    assert "我手工改过的正文" not in " ".join(values)


def test_previewed_draft_is_snapshotted_and_preview_does_not_change_the_hash(tmp_path, monkeypatch):
    cfg = gen_cfg(tmp_path, monkeypatch, notes="修复解析器\n")
    path, first = cli_module._generate(cfg, "daily", DAY, "extractive")
    cli_module._preview(cfg, path)
    marked = json.loads(path.read_text(encoding="utf-8"))
    assert marked["previewed_at"] and marked["preview_count"] == 1
    assert marked["hash"] == first["hash"], "preview marks must not invalidate a confirmation hash"
    _, _, info = cli_module._generate_impl(cfg, "daily", DAY, "extractive")
    assert info["action"] == "snapshot" and info["archive"]


def test_protect_policy_refuses_without_force_and_allows_with(tmp_path, monkeypatch):
    cfg = gen_cfg(tmp_path, monkeypatch, notes="修复解析器\n")
    cfg["policy"] = {"draft_overwrite": "protect"}
    path, _ = cli_module._generate(cfg, "daily", DAY, "extractive")
    cli_module._preview(cfg, path)
    with pytest.raises(SubmissionBlocked, match="protected"):
        cli_module._generate_impl(cfg, "daily", DAY, "extractive")
    _, _, info = cli_module._generate_impl(cfg, "daily", DAY, "extractive", force=True)
    assert info["action"] == "snapshot"


def test_submitted_draft_is_never_replaced(tmp_path, monkeypatch):
    cfg = gen_cfg(tmp_path, monkeypatch, notes="修复解析器\n")
    path, _ = cli_module._generate(cfg, "daily", DAY, "extractive")
    current = json.loads(path.read_text(encoding="utf-8"))
    current["submission"] = {"status": "verified", "report_id": "report-1"}
    atomic_json(path, current)
    with pytest.raises(SubmissionBlocked, match="submit attempt"):
        cli_module._generate(cfg, "daily", DAY, "extractive")


def test_ai_failure_keeps_the_existing_draft(tmp_path, monkeypatch):
    cfg = gen_cfg(tmp_path, monkeypatch, notes="修复解析器\n")
    path, _ = cli_module._generate(cfg, "daily", DAY, "extractive")
    before = _draft_bytes(path)
    monkeypatch.setattr(cli_module, "synthesize",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("Codex synthesis unavailable")))
    with pytest.raises(ValueError):
        cli_module._generate(cfg, "daily", DAY, "codex")
    assert _draft_bytes(path) == before, "a failed synthesis must never replace a good draft"


# --------------------------------------------------------------------------
# P1-10 / P1-11 evidence retention and freshness
# --------------------------------------------------------------------------

def test_evidence_records_collection_metadata(tmp_path, monkeypatch):
    cfg = gen_cfg(tmp_path, monkeypatch, notes="修复解析器\n")
    path, result = cli_module._collect(cfg, "daily", DAY)
    assert json.loads(path.read_text(encoding="utf-8"))["collected_at"]
    assert result["deduped_fact_count"] == len(result["facts"])
    assert result["retention"]["action"] == "write"
    sources = {s["name"]: s for s in result["sources"]}
    assert sources["codex"]["configured"] is True and sources["codex"]["reachable"] is True
    assert sources["markdown"]["fact_count"] == 1
    assert sources["git"]["configured"] is False


def test_unavailable_source_does_not_overwrite_good_evidence(tmp_path, monkeypatch):
    cfg = gen_cfg(tmp_path, monkeypatch, notes="修复解析器\n")
    path, first = cli_module._collect(cfg, "daily", DAY)
    assert len(first["facts"]) >= 1
    before = path.read_bytes()
    # The notes directory disappears (unmounted drive, moved folder, bad scope).
    for child in list((tmp_path / "notes").iterdir()):
        child.unlink()
    (tmp_path / "notes").rmdir()
    path2, second = cli_module._collect(cfg, "daily", DAY)
    assert path2 == path
    assert path.read_bytes() == before, "a read failure must not replace stored evidence"
    assert second["retention"]["action"] == "retain"
    assert second["retention"]["previous_fact_count"] == len(first["facts"])
    rejected = list((tmp_path / "data" / "diagnostics").glob("evidence-*.json"))
    assert rejected, "the rejected collection must be recorded for diagnosis"


def test_deliberate_scope_narrowing_still_writes(tmp_path, monkeypatch):
    """A real result (everything reachable, nothing in scope) is not a failure."""
    cfg = gen_cfg(tmp_path, monkeypatch, notes="修复解析器\n")
    cli_module._collect(cfg, "daily", DAY)
    cfg["codex"]["project_paths"] = [str(tmp_path / "elsewhere")]
    cfg["supplemental"]["markdown_paths"] = []
    _, result = cli_module._collect(cfg, "daily", DAY)
    assert result["retention"]["action"] == "write"
    assert result["deduped_fact_count"] == 0
