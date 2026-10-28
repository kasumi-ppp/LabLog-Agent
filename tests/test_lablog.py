import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from lablog.sources import collect_codex, collect_markdown, period_bounds
from lablog.workflow import freeze_draft, validate_submission, SubmissionBlocked
from lablog.dws import template_fingerprint, encode_contents, DwsBlocked, DwsClient
from lablog.report import build_draft
from lablog.cli import _approval_context, _deduplicate_facts, _outbox_history, _preflight, _preview_markdown, _readback_matches, _report_period, _submit, _reconcile, _verify_recipients
from lablog.config import is_workday, schedule_due
from lablog.ai import validate_ai_result


def test_daily_bounds_use_china_time():
    start, end = period_bounds("daily", date(2026, 10, 13))
    assert start.isoformat() == "2026-10-12T16:00:00+00:00"
    assert end.isoformat() == "2026-10-13T16:00:00+00:00"


def test_collect_deduplicates_events_and_excludes_private_messages(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    session = root / "s.jsonl"
    lines = [
        {"type": "session_meta", "payload": {"id": "s", "cwd": "E:/work/project"}},
        {"type": "response_item", "timestamp": "2026-10-13T01:00:00Z", "payload": {"type": "message", "role": "assistant", "channel": "final", "content": [{"type": "output_text", "text": "Completed parser"}]}},
        {"type": "event_msg", "timestamp": "2026-10-13T01:00:00Z", "payload": {"type": "agent_message", "message": "Completed parser"}},
        {"type": "response_item", "timestamp": "2026-10-13T02:00:00Z", "payload": {"type": "reasoning", "summary": [{"text": "secret"}]}},
        {"type": "response_item", "timestamp": "2026-10-13T03:00:00Z", "payload": {"type": "function_call", "arguments": "token=secret"}},
    ]
    session.write_text("\n".join(json.dumps(x) for x in lines) + "\n{partial", encoding="utf-8")
    result = collect_codex([root], "daily", date(2026, 10, 13), project_paths=["E:/work/project"])
    assert len(result["facts"]) == 1
    assert result["facts"][0]["text"] == "Completed parser"
    assert "partial" in " ".join(result["warnings"])
    assert "secret" not in json.dumps(result)


def test_freeze_hash_detects_edits():
    draft = {"kind": "daily", "date": "2026-10-13", "fields": [{"name": "完成工作", "value": "A"}]}
    frozen = freeze_draft(draft)
    assert frozen["hash"]
    frozen["fields"][0]["value"] = "B"
    with pytest.raises(SubmissionBlocked, match="hash"):
        validate_submission(frozen, frozen["hash"], {"template_fingerprint": "x"})


def test_submit_requires_hash_and_live_template_match():
    draft = freeze_draft({"kind": "daily", "date": "2026-10-13", "template_fingerprint": "a", "fields": []})
    with pytest.raises(SubmissionBlocked, match="confirm"):
        validate_submission(draft, None, {"template_fingerprint": "a"})
    with pytest.raises(SubmissionBlocked, match="template"):
        validate_submission(draft, draft["hash"], {"template_fingerprint": "b"})


def test_template_fingerprint_is_sorted_and_detects_field_change():
    template = {"report_template_id": "t", "lastModifiedTime": 1, "report_template_fields": [
        {"field_name": "B", "field_sort": 2, "field_type": 1},
        {"field_name": "A", "field_sort": 0, "field_type": 1}]}
    ordered = {**template, "report_template_fields": list(reversed(template["report_template_fields"]))}
    assert template_fingerprint(template) == template_fingerprint(ordered)
    changed = {**template, "report_template_fields": [{"field_name": "A", "field_sort": 0, "field_type": 2}]}
    assert template_fingerprint(template) != template_fingerprint(changed)


def test_date_encoding_unknown_blocks_submission():
    template = {"report_template_fields": [{"field_name": "工作日期", "field_sort": 0, "field_type": 5}]}
    draft = {"fields": [{"name": "工作日期", "type": 5, "value": "2026-10-13"}]}
    with pytest.raises(DwsBlocked, match="date"):
        encode_contents(draft, template, {})


def test_daily_manual_quote_is_verbatim_and_hours_unset():
    template = {"report_template_id": "t", "report_template_fields": [
        {"field_name": "今日完成", "field_sort": 0, "field_type": 1},
        {"field_name": "心得体会", "field_sort": 1, "field_type": 1},
        {"field_name": "总工时/h", "field_sort": 2, "field_type": 2},
        {"field_name": "工作日期", "field_sort": 3, "field_type": 5}]}
    quote = "  原文第一行\n第二行。\n"
    evidence = {"facts": [{"evidence_id": "x", "source": "git", "status": "committed", "text": "Add parser", "time": "2026-10-13T09:00:00+08:00"}]}
    draft = build_draft("daily", "2026-10-13", template, evidence, manual_text=quote)
    fields = {x["name"]: x["value"] for x in draft["fields"]}
    assert fields["心得体会"] == quote
    assert "总工时/h" not in fields
    assert "Add parser" in fields["今日完成"]
    assert fields["工作日期"] == "2026-10-13"


def test_reader_body_strips_traceability_marks_into_draft_citations():
    template = {"report_template_id": "t", "report_template_fields": [
        {"field_name": "今日完成", "field_sort": 0, "field_type": 1},
        {"field_name": "心得体会", "field_sort": 1, "field_type": 1}]}
    evidence = {"facts": [{"evidence_id": "abc", "source": "git", "status": "committed",
                           "text": "Fix parser", "time": "2026-10-13T09:00:00+08:00"}]}
    ai_result = {"completed": "- 修复解析器 [证据:abc]", "summary": "今日修复解析器 [证据:abc]",
                 "plan": "", "citations": ["abc"]}
    draft = build_draft("daily", "2026-10-13", template, evidence, ai_result=ai_result)
    fields = {x["name"]: x["value"] for x in draft["fields"]}
    assert "[证据:" not in fields["今日完成"]
    assert "[证据:" not in fields["心得体会"]
    assert "修复解析器" in fields["今日完成"]
    assert draft["citations"] == {"今日完成": ["abc"], "心得体会": ["abc"]}

    codex_fact = {"evidence_id": "c1", "source": "codex", "status": "completed",
                  "text": "Done thing", "time": "2026-10-13T10:00:00+08:00"}
    extractive = build_draft("daily", "2026-10-13", template, {"facts": [codex_fact]})
    body = {x["name"]: x["value"] for x in extractive["fields"]}["今日完成"]
    assert "Done thing" in body
    assert "未独立核验" not in body and "[证据:" not in body


def test_selected_project_mode_without_session_id_is_allowed(tmp_path, monkeypatch):
    from lablog.cli import _collect
    cfg = {"_base": str(tmp_path), "data_dir": "data", "codex": {
        "mode": "selected", "project_paths": [str(tmp_path)], "session_roots": []}}
    path, result = _collect(cfg, "daily", "2026-10-13")
    assert path.is_file()
    assert result["facts"] == []


def test_markdown_uses_declared_date_not_filesystem_mtime(tmp_path):
    old = tmp_path / "2026-10-12-note.md"
    old.write_text("Finished old work", encoding="utf-8")
    today = tmp_path / "today.md"
    today.write_text("---\ndate: 2026-10-13\nstatus: completed\n---\nFinished today", encoding="utf-8")
    undated = tmp_path / "note.md"
    undated.write_text("Finished without date", encoding="utf-8")
    result = collect_markdown([tmp_path], "daily", date(2026, 10, 13))
    assert [x["text"] for x in result["facts"]] == ["Finished today"]
    assert result["facts"][0]["status"] == "completed"
    assert any("date" in x for x in result["warnings"])


def _ready_submission(tmp_path, monkeypatch, *, outcome="verified", allow_add_receivers=1,
                      fixed_recipients=None):
    import lablog.cli as cli
    from lablog.workflow import atomic_json
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    template = {"report_template_id": "t", "report_template_name": "学生日报",
                "allowAddReceivers": allow_add_receivers,
                "report_template_fields": [{"field_name": "今日完成", "field_sort": 0, "field_type": 1}]}
    daily_template = {"id": "t", "name": "学生日报"}
    # fixed_recipients lives in the template config, which _approval_context hashes,
    # so it must be set before the draft is frozen.
    if fixed_recipients is not None:
        daily_template["fixed_recipients"] = fixed_recipients
    cfg = {"_base": str(tmp_path), "data_dir": "data", "dws": {"profile": "p"},
           "identity": {"corp_id": "corp", "user_id": "self", "corp_name": "lab", "user_name": "Self"},
           "recipients": [{"name": "Recipient", "user_id": "r", "corp_name": "lab"}],
           "templates": {"daily": daily_template}, "history_from": today}
    draft = freeze_draft({"kind": "daily", "date": today, "template_id": "t", "template_name": "学生日报",
                          "template_fingerprint": template_fingerprint(template),
                          "fields": [{"name": "今日完成", "type": 1, "value": "Done"}], "blockers": [],
                          "approval_context": _approval_context(cfg, "daily", today)})
    path = tmp_path / "draft.json"
    atomic_json(path, draft)

    class FakeDws:
        submits = 0
        def auth(self):
            return {"corp_id": "corp", "user_id": "self", "user_name": "Self"}
        def template(self, name, ident):
            return template
        def contact(self, ident):
            return {"orgUserName": "Recipient", "orgName": "lab"}
        def outbox(self, name, start, end):
            return []
        def submit(self, ident, contents, users):
            self.submits += 1
            if outcome == "timeout":
                raise DwsBlocked("DWS timeout; outcome unknown")
            return "report-1"
        def detail(self, ident):
            if ident == "existing":
                return {"report_template_id": "t", "work_period": {"start": today, "end": today}}
            return {"report_template_id": "t", "contents": [{"key": "今日完成", "content": "Wrong" if outcome == "mismatch" else "Done"}]}
    fake = FakeDws()
    monkeypatch.setattr(cli, "_client", lambda config: fake)
    return cfg, path, draft, fake


def test_unknown_submit_is_not_retried(tmp_path, monkeypatch):
    cfg, path, draft, fake = _ready_submission(tmp_path, monkeypatch, outcome="timeout")
    with pytest.raises(DwsBlocked, match="unknown"):
        _submit(cfg, path, draft["hash"])
    assert json.loads(path.read_text(encoding="utf-8"))["submission"]["status"] == "unknown"
    with pytest.raises(SubmissionBlocked):
        _submit(cfg, path, draft["hash"])
    assert fake.submits == 1


def test_readback_mismatch_blocks_success(tmp_path, monkeypatch):
    cfg, path, draft, _ = _ready_submission(tmp_path, monkeypatch, outcome="mismatch")
    with pytest.raises(SubmissionBlocked, match="readback"):
        _submit(cfg, path, draft["hash"])
    assert json.loads(path.read_text(encoding="utf-8"))["submission"]["status"] == "mismatch"


def test_readback_rejects_wrong_report_id_type_and_sort():
    expected = [{"key": "今日完成", "type": "1", "sort": "0", "content": "Done"}]
    assert not _readback_matches({"report_template_id": "t", "reportId": "other",
                                  "contents": [{"key": "今日完成", "type": "1", "sort": "0", "content": "Done"}]}, "t", expected, "expected")
    assert not _readback_matches({"report_template_id": "t", "reportId": "expected",
                                  "contents": [{"key": "今日完成", "type": "9", "sort": "0", "content": "Done"}]}, "t", expected, "expected")
    assert not _readback_matches({"report_template_id": "t",
                                  "contents": [{"key": "今日完成", "content": "Done"}, {"key": "今日完成", "content": "Done"}]}, "t", expected)


def test_copied_draft_cannot_bypass_one_time_period_guard(tmp_path, monkeypatch):
    cfg, path, draft, fake = _ready_submission(tmp_path, monkeypatch)
    assert _submit(cfg, path, draft["hash"])["status"] == "verified"
    copied = tmp_path / "copied.json"
    copied.write_text(json.dumps(draft), encoding="utf-8")
    with pytest.raises(SubmissionBlocked, match="period"):
        _submit(cfg, copied, draft["hash"])
    assert fake.submits == 1


def test_live_template_drift_blocks_before_submit(tmp_path, monkeypatch):
    cfg, path, draft, fake = _ready_submission(tmp_path, monkeypatch)
    fake.template = lambda name, ident: {"report_template_id": "t", "report_template_name": "学生日报",
                                         "allowAddReceivers": 1, "report_template_fields": [
                                             {"field_name": "今日完成", "field_sort": 1, "field_type": 1}]}
    with pytest.raises(SubmissionBlocked, match="template drift"):
        _submit(cfg, path, draft["hash"])
    assert fake.submits == 0


def test_recipient_mismatch_blocks_before_submit(tmp_path, monkeypatch):
    cfg, path, draft, fake = _ready_submission(tmp_path, monkeypatch)
    fake.contact = lambda ident: {"orgUserName": "Other", "orgName": "lab"}
    with pytest.raises(SubmissionBlocked, match="recipient"):
        _submit(cfg, path, draft["hash"])
    assert fake.submits == 0


def test_incomplete_remote_pagination_is_not_treated_as_empty(monkeypatch):
    client = DwsClient("dws.exe", "profile")
    monkeypatch.setattr(client, "call", lambda *a, **k: {"ok": True, "outcome": "success",
                        "data": {"complete": False, "reports": []}, "meta": {"pagination": {"endpoint_exhausted": False}}})
    with pytest.raises(DwsBlocked, match="pagination"):
        client.outbox("学生日报", "2026-10-01T00:00:00+08:00", "2026-10-02T00:00:00+08:00")


def test_weekly_duplicate_history_must_cover_monday():
    cfg = {"history_from": "2026-10-13"}
    class NeverQueried:
        def outbox(self, *args):
            raise AssertionError("incomplete history must block before the remote query")
    with pytest.raises(SubmissionBlocked, match="cover report period"):
        _outbox_history(cfg, NeverQueried(), "学生周报", "2026-10-16", "weekly")


def test_exact_cross_source_duplicates_merge_without_merging_statuses():
    facts = [
        {"evidence_id": "codex-1", "source": "codex", "source_path": "session.jsonl",
         "status": "completed", "confidence": "medium", "text": "Finished adding the report parser."},
        {"evidence_id": "note-1", "source": "markdown", "source_path": "2026-10-13.md",
         "status": "completed", "confidence": "high", "text": "finished adding the report parser"},
        {"evidence_id": "plan-1", "source": "codex", "status": "planned",
         "confidence": "low", "text": "Finished adding the report parser."},
    ]
    deduplicated = _deduplicate_facts(facts)
    assert len(deduplicated) == 2
    completed = next(item for item in deduplicated if item["status"] == "completed")
    assert set(completed["evidence_ids"]) == {"codex-1", "note-1"}
    assert completed["source"] == "markdown"
    assert next(item for item in deduplicated if item["status"] == "planned")["evidence_id"] == "plan-1"


def test_preview_includes_full_body_quality_warnings_and_duplicate_scope():
    draft = {"kind": "daily", "date": "2026-10-13", "hash": "abc", "template_name": "学生日报",
             "template_fingerprint": "fp", "fields": [{"name": "心得体会", "value": "原文第一段。\n\n原文第二段。"}],
             "blockers": ["date encoding unverified"], "warnings": ["工时留空"]}
    context = {"identity": {"corp_name": "lab", "user_name": "Self"},
               "recipients": [{"name": "Recipient Name"}]}
    markdown = _preview_markdown(draft, context, {"status": "unknown", "history_from": "2026-10-01",
                                                  "checked_through": "2026-10-13T18:00:00+08:00",
                                                  "reason": "period could not be verified"})
    assert "原文第一段。\n\n原文第二段。" in markdown
    assert "2026-10-13 至 2026-10-13" in markdown
    assert "Recipient Name" in markdown and "学生日报" in markdown
    assert "提交状态: 未提交" in markdown
    assert "工时留空" in markdown and "date encoding unverified" in markdown
    assert "2026-10-01 至 2026-10-13T18:00:00+08:00" in markdown
    assert "period could not be verified" in markdown


def test_period_marker_round_trips_from_a_report_content_field():
    from lablog.periods import format_period_marker, parse_period_marker
    marker = format_period_marker("weekly", "2026-10-13")
    assert marker == "统计周期：2026-10-12 至 2026-10-18"
    assert parse_period_marker("本周工作：\n" + marker + "\n完成实验记录") == ("2026-10-12", "2026-10-18")
    class DetailClient:
        def detail(self, report_id):
            return {"contents": [{"key": "本周完成工作", "content": marker}]}
    assert _report_period({"reportId": "r"}, DetailClient(), "学生周报") == ("2026-10-12", "2026-10-18")


def test_doctor_and_submission_share_duplicate_recipient_validation():
    cfg = {"identity": {"corp_name": "lab"}, "recipients": [
        {"name": "Recipient", "user_id": "r", "corp_name": "lab"},
        {"name": "Recipient", "user_id": "r", "corp_name": "lab"}]}
    class ContactClient:
        calls = 0
        def contact(self, user_id):
            self.calls += 1
            return {"orgUserName": "Recipient", "orgName": "lab"}
    client = ContactClient()
    user_ids, results, blockers = _verify_recipients(cfg, client)
    assert user_ids == ["r"]
    assert [x["verified"] for x in results] == [True, False]
    assert blockers == ["recipient ID missing, duplicated or unlabeled"]
    assert client.calls == 1


@pytest.mark.parametrize("kind,day,start,end", [
    ("daily", date(2026, 10, 13), "2026-10-12T16:00:00+00:00", "2026-10-13T16:00:00+00:00"),
    ("weekly", date(2026, 10, 13), "2026-10-11T16:00:00+00:00", "2026-10-18T16:00:00+00:00"),
    ("monthly", date(2026, 12, 31), "2026-11-30T16:00:00+00:00", "2026-12-31T16:00:00+00:00"),
])
def test_period_windows(kind, day, start, end):
    got_start, got_end = period_bounds(kind, day)
    assert (got_start.isoformat(), got_end.isoformat()) == (start, end)


def test_typed_number_and_verified_date_encoding():
    template = {"report_template_fields": [
        {"field_name": "工作日期", "field_sort": 2, "field_type": 5},
        {"field_name": "总工时/h", "field_sort": 1, "field_type": 2},
        {"field_name": "心得体会", "field_sort": 0, "field_type": 1}]}
    draft = {"fields": [{"name": "工作日期", "type": 5, "value": "2026-10-13"},
                        {"name": "总工时/h", "type": 2, "value": "2.50"},
                        {"name": "心得体会", "type": 1, "value": "原文\n末行\n"}]}
    encoded = encode_contents(draft, template, {"verified": True, "format": "iso-date"})
    assert [(x["key"], x["content"], x["contentType"]) for x in encoded] == [
        ("心得体会", "原文\n末行\n", "markdown"), ("总工时/h", "2.50", "origin"),
        ("工作日期", "2026-10-13", "origin")]


def test_success_without_report_id_is_unknown(monkeypatch):
    client = DwsClient("dws.exe", "profile")
    monkeypatch.setattr(client, "call", lambda *a, **k: {"errcode": 0, "success": True, "result": {}})
    with pytest.raises(DwsBlocked, match="no report ID"):
        client.submit("t", [], ["r"])


def test_wrong_account_blocks_before_submit(tmp_path, monkeypatch):
    cfg, path, draft, fake = _ready_submission(tmp_path, monkeypatch)
    fake.auth = lambda: {"corp_id": "other", "user_id": "self"}
    with pytest.raises(SubmissionBlocked, match="identity"):
        _submit(cfg, path, draft["hash"])
    assert fake.submits == 0


def test_receiver_permission_blocks_before_submit(tmp_path, monkeypatch):
    cfg, path, draft, fake = _ready_submission(tmp_path, monkeypatch)
    original = fake.template
    fake.template = lambda name, ident: {**original(name, ident), "allowAddReceivers": 0}
    with pytest.raises(SubmissionBlocked, match="template drift"):
        _submit(cfg, path, draft["hash"])
    assert fake.submits == 0


def test_receiver_restriction_is_a_real_blocker_not_a_test_bypass(tmp_path, monkeypatch):
    cfg, _, draft, fake = _ready_submission(tmp_path, monkeypatch)
    original = fake.template

    def restricted(name, ident):
        return {**original(name, ident), "allowAddReceivers": 0}

    fake.template = restricted
    # Re-freeze using the restricted live template so this reaches the explicit
    # receiver-semantics gate instead of being classified as template drift.
    draft["template_fingerprint"] = template_fingerprint(restricted("学生日报", "t"))
    from lablog.workflow import atomic_json
    atomic_json(tmp_path / "draft.json", freeze_draft(draft))
    with pytest.raises(SubmissionBlocked, match="no fixed recipient list is declared"):
        _preflight(cfg, draft)
    assert fake.submits == 0


def test_fixed_receiver_list_matching_config_passes_preflight(tmp_path, monkeypatch):
    """allowAddReceivers=0 is not a blanket block: an exactly matching declared
    list keeps every recipient verified through client.contact()."""
    cfg, _, draft, fake = _ready_submission(
        tmp_path, monkeypatch, allow_add_receivers=0,
        fixed_recipients=[{"name": "Recipient", "user_id": "r", "corp_name": "lab"}])
    assert draft["template_fingerprint"] == template_fingerprint(fake.template("学生日报", "t"))
    client, _template, user_ids, contents = _preflight(cfg, draft)
    assert user_ids == ["r"]
    assert [item["key"] for item in contents] == ["今日完成"]
    assert fake.submits == 0


@pytest.mark.parametrize("fixed_recipients", [
    [{"name": "Recipient", "user_id": "r", "corp_name": "lab"},
     {"name": "Second", "user_id": "r2", "corp_name": "lab"}],
    [{"name": "Other", "user_id": "r9", "corp_name": "lab"}],
])
def test_fixed_receiver_list_mismatch_blocks_preflight(tmp_path, monkeypatch, fixed_recipients):
    """Adding or dropping a name against a template-fixed list stays blocked."""
    cfg, _, draft, fake = _ready_submission(
        tmp_path, monkeypatch, allow_add_receivers=0, fixed_recipients=fixed_recipients)
    with pytest.raises(SubmissionBlocked, match="do not match the template's fixed receiver list"):
        _preflight(cfg, draft)
    assert fake.submits == 0


def test_fixed_receiver_list_with_duplicate_ids_blocks_preflight(tmp_path, monkeypatch):
    cfg, _, draft, fake = _ready_submission(
        tmp_path, monkeypatch, allow_add_receivers=0,
        fixed_recipients=[{"name": "Recipient", "user_id": "r", "corp_name": "lab"},
                          {"name": "Recipient Again", "user_id": "r", "corp_name": "lab"}])
    with pytest.raises(SubmissionBlocked, match="fixed recipient list has duplicates"):
        _preflight(cfg, draft)
    assert fake.submits == 0


def test_possible_remote_duplicate_blocks_before_submit(tmp_path, monkeypatch):
    cfg, path, draft, fake = _ready_submission(tmp_path, monkeypatch)
    fake.outbox = lambda *a: [{"reportId": "existing"}]
    with pytest.raises(SubmissionBlocked, match="remote report"):
        _submit(cfg, path, draft["hash"])
    assert fake.submits == 0


def test_old_report_in_another_period_does_not_block_new_period(tmp_path, monkeypatch):
    cfg, path, draft, fake = _ready_submission(tmp_path, monkeypatch)
    fake.outbox = lambda *a: [{"reportId": "existing"}]
    other = (date.fromisoformat(draft["date"]) - timedelta(days=1)).isoformat()
    fake_detail = fake.detail
    fake.detail = lambda ident: ({"report_template_id": "t", "work_period": {"start": other, "end": other}}
                                 if ident == "existing" else fake_detail(ident))
    assert _submit(cfg, path, draft["hash"])["status"] == "verified"


def test_reconcile_requires_original_identity_and_approved_context(tmp_path, monkeypatch):
    cfg, path, draft, _ = _ready_submission(tmp_path, monkeypatch, outcome="timeout")
    with pytest.raises(DwsBlocked):
        _submit(cfg, path, draft["hash"])
    cfg["dws"]["profile"] = "different"
    assert _reconcile(cfg, path)["status"] == "manual_review_required"


def test_state_guard_uses_the_full_report_period():
    from lablog.cli import _state_path
    cfg = {"data_dir": "data", "_base": "E:/lab"}
    first = {"approval_context": {"profile": "p"}, "template_id": "t", "kind": "weekly", "date": "2026-10-12"}
    last = {"approval_context": {"profile": "p"}, "template_id": "t", "kind": "weekly", "date": "2026-10-16"}
    assert _state_path(cfg, first) == _state_path(cfg, last)


def test_interrupted_submit_records_unknown_and_never_retries(tmp_path, monkeypatch):
    cfg, path, draft, fake = _ready_submission(tmp_path, monkeypatch)
    def interrupt(*args):
        fake.submits += 1
        raise KeyboardInterrupt
    fake.submit = interrupt
    with pytest.raises(KeyboardInterrupt):
        _submit(cfg, path, draft["hash"])
    assert json.loads(path.read_text(encoding="utf-8"))["submission"]["status"] == "unknown"
    with pytest.raises(SubmissionBlocked):
        _submit(cfg, path, draft["hash"])
    assert fake.submits == 1


def test_calendar_uses_explicit_holidays_makeup_and_year(tmp_path):
    cal = tmp_path / "cal.json"
    cal.write_text(json.dumps({"year": 2026, "source": "official", "holidays": ["2026-10-13"],
                               "makeup_workdays": ["2026-10-18"]}), encoding="utf-8")
    cfg = {"_base": str(tmp_path), "calendar": {"file": "cal.json"}, "schedule": {"auto_submit": False}}
    assert not is_workday(cfg, date(2026, 10, 13))
    assert is_workday(cfg, date(2026, 10, 18))
    assert not schedule_due(cfg, "daily", date(2026, 10, 13))
    with pytest.raises(ValueError, match="calendar year"):
        schedule_due(cfg, "daily", date(2027, 10, 13))


def test_assisted_ai_rejects_planned_completion_and_new_metrics():
    facts = [{"evidence_id": "a", "status": "planned", "text": "Plan parser in 2 stages"},
             {"evidence_id": "b", "status": "completed", "text": "Finished parser in 2 stages"}]
    with pytest.raises(ValueError, match="completed evidence"):
        validate_ai_result({"completed": "Finished parser [证据:a]", "summary": "", "plan": "", "citations": ["a"]}, facts)
    with pytest.raises(ValueError, match="unsupported numbers"):
        validate_ai_result({"completed": "Finished parser in 3 stages [证据:b]", "summary": "", "plan": "", "citations": ["b"]}, facts)


def test_assisted_ai_accepts_cited_completed_fact():
    facts = [{"evidence_id": "b", "status": "completed", "text": "Finished parser in 2 stages"}]
    result = validate_ai_result({"completed": "Finished parser in 2 stages [证据:b]",
                                 "summary": "Parser result recorded [证据:b]", "plan": "", "citations": ["b"]}, facts)
    assert result["citations"] == ["b"]


def test_assisted_ai_rejects_planned_claim_laundered_with_completed_citation():
    facts = [{"evidence_id": "p", "status": "planned", "text": "Plan production deployment"},
             {"evidence_id": "c", "status": "completed", "text": "Added local documentation"}]
    with pytest.raises(ValueError, match="completed evidence"):
        validate_ai_result({"completed": "Completed production deployment and local documentation [证据:p] [证据:c]",
                            "summary": "", "plan": "", "citations": ["p", "c"]}, facts)


def test_assisted_ai_rejects_planned_evidence_in_completed_summary():
    facts = [{"evidence_id": "p", "status": "planned", "text": "Plan production deployment"}]
    with pytest.raises(ValueError, match="planned evidence as completed"):
        validate_ai_result({"completed": "", "summary": "Completed production deployment [证据:p]",
                            "plan": "", "citations": ["p"]}, facts)


@pytest.mark.parametrize("claim", ["完成部署生产环境", "已部署生产环境", "部署完成并迁移数据"])
def test_assisted_ai_rejects_common_deployment_completion_claims_from_planned_evidence(claim):
    facts = [{"evidence_id": "p", "status": "planned", "text": "计划部署生产环境并迁移数据"}]
    with pytest.raises(ValueError, match="planned evidence as completed"):
        validate_ai_result({"completed": "", "summary": f"{claim} [证据:p]",
                            "plan": "", "citations": ["p"]}, facts)


@pytest.mark.parametrize("claim", ["尚未完成部署生产环境", "没有完成部署生产环境", "计划完成部署生产环境",
                                   "not completed production deployment", "并不算已完成部署生产环境",
                                   "并不认为已完成部署生产环境"])
def test_assisted_ai_accepts_negated_or_planned_deployment_summary(claim):
    facts = [{"evidence_id": "p", "status": "planned", "text": "计划部署生产环境"}]
    result = validate_ai_result({"completed": "", "summary": f"{claim} [证据:p]",
                                 "plan": "", "citations": ["p"]}, facts)
    assert result["summary"].startswith(claim)


def test_assisted_ai_rejects_claim_unrelated_to_its_citation():
    facts = [{"evidence_id": "x", "status": "completed", "text": "完成本地文档校验"}]
    with pytest.raises(ValueError, match="lexical support"):
        validate_ai_result({"completed": "已部署生产环境并完成数据迁移 [证据:x]",
                            "summary": "", "plan": "", "citations": ["x"]}, facts)


def test_reconcile_known_id_verifies_exact_readback(tmp_path, monkeypatch):
    cfg, path, draft, fake = _ready_submission(tmp_path, monkeypatch, outcome="mismatch")
    with pytest.raises(SubmissionBlocked):
        _submit(cfg, path, draft["hash"])
    fake.detail = lambda ident: {"report_template_id": "t", "contents": [{"key": "今日完成", "content": "Done"}]}
    assert _reconcile(cfg, path)["status"] == "verified"
    assert json.loads(path.read_text(encoding="utf-8"))["submission"]["status"] == "verified"


def test_codex_skips_injected_history_and_keeps_assistant_progress(tmp_path):
    session = tmp_path / "s.jsonl"
    rows = [{"type": "session_meta", "payload": {"id": "s", "cwd": "E:/work/project"}},
            {"type": "response_item", "timestamp": "2026-10-13T01:00:00Z", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text",
                "text": "The following is the Codex agent history added since your last approval assessment\n[12] tool transcript gAAAA"}]}},
            {"type": "response_item", "timestamp": "2026-10-13T02:00:00Z", "payload": {
                "type": "message", "role": "assistant", "phase": "commentary",
                "content": [{"type": "output_text", "text": "正在核对模板字段。"}]}}]
    session.write_text("\n".join(json.dumps(x) for x in rows), encoding="utf-8")
    result = collect_codex([session], "daily", date(2026, 10, 13), project_paths=["E:/work/project"])
    assert [f["text"] for f in result["facts"]] == ["正在核对模板字段。"]
    assert result["facts"][0]["status"] == "in_progress"


@pytest.mark.parametrize("response", [{"success": True}, {"dry_run": True, "executed": True, "tool": "create_report"}])
def test_dws_dry_run_requires_explicit_nonexecution(monkeypatch, response):
    client = DwsClient("dws.exe", "profile")
    monkeypatch.setattr(client, "call", lambda *a, **k: response)
    with pytest.raises(DwsBlocked, match="dry-run"):
        client.submit("t", [], ["r"], dry_run=True)


def test_codex_excludes_approval_reviewer_responses(tmp_path):
    rows = [{"type": "session_meta", "payload": {"id": "s", "cwd": "E:/work/project"}},
            {"type": "response_item", "timestamp": "2026-10-13T01:00:00Z", "payload": {
                "type": "message", "role": "assistant", "content": [{"type": "output_text", "text":
                '{"risk_level":"low","user_authorization":"high","outcome":"allow","rationale":"policy"}'}]}}]
    session = tmp_path / "s.jsonl"
    session.write_text("\n".join(json.dumps(x) for x in rows), encoding="utf-8")
    assert collect_codex([session], "daily", date(2026, 10, 13), project_paths=["E:/work/project"])["facts"] == []


def test_generate_recollects_after_project_scope_changes(tmp_path, monkeypatch):
    from lablog.cli import _generate
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    session = session_root / "s.jsonl"
    session.write_text(json.dumps({"type": "session_meta", "payload": {"id": "s", "cwd": "E:/work/private"}})
                       + "\n" + json.dumps({"type": "response_item", "timestamp": "2026-10-13T01:00:00Z", "payload": {
                           "type": "message", "role": "assistant", "phase": "final_answer",
                           "content": [{"type": "output_text", "text": "已完成私有工作"}]}}), encoding="utf-8")
    cfg = {"_base": str(tmp_path), "data_dir": "data", "dws": {"profile": "p"},
           "codex": {"mode": "selected", "session_roots": [str(session_root)], "project_paths": ["E:/work/private"]},
           "supplemental": {}, "templates": {"daily": {"id": "t", "name": "学生日报"}}, "quality": {}}
    template = {"report_template_id": "t", "report_template_name": "学生日报", "lastModifiedTime": 1,
                "allowAddReceivers": 1, "report_template_fields": [
                    {"field_name": "今日完成", "field_sort": 0, "field_type": 1},
                    {"field_name": "心得体会", "field_sort": 1, "field_type": 1}]}
    class FakeDws:
        def template(self, *args): return template
    monkeypatch.setattr("lablog.cli._client", lambda _: FakeDws())
    from lablog.cli import _collect
    _collect(cfg, "daily", "2026-10-13")
    cfg["codex"]["project_paths"] = ["E:/work/other"]
    _, draft = _generate(cfg, "daily", "2026-10-13", "extractive")
    assert "已完成私有工作" not in json.dumps(draft, ensure_ascii=False)


def test_period_state_key_is_shared_across_week_days():
    from lablog.cli import _state_path
    cfg = {"data_dir": "data", "_base": "E:/lab"}
    assert _state_path(cfg, {"approval_context": {"profile": "p"}, "template_id": "t", "kind": "weekly", "date": "2026-10-12"}) == _state_path(
        cfg, {"approval_context": {"profile": "p"}, "template_id": "t", "kind": "weekly", "date": "2026-10-16"})


def test_unknown_future_assistant_message_does_not_count_as_completed():
    template = {"report_template_id": "t", "report_template_fields": [
        {"field_name": "今日完成", "field_sort": 0, "field_type": 1},
        {"field_name": "明日计划", "field_sort": 1, "field_type": 1},
        {"field_name": "心得体会", "field_sort": 2, "field_type": 1}]}
    evidence = {"facts": [{"source": "codex", "role": "assistant", "status": "unknown",
                            "evidence_id": "p", "text": "明天准备复现模型。"}]}
    draft = build_draft("daily", "2026-10-13", template, evidence)
    assert "明天准备复现模型" not in next(x["value"] for x in draft["fields"] if x["name"] == "今日完成")
    assert "no dated completion evidence" in draft["blockers"]


def test_codex_collection_warns_when_session_file_cap_truncates(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    for name in ("a.jsonl", "b.jsonl"):
        (root / name).write_text("{}\n", encoding="utf-8")
    result = collect_codex([root], "daily", date(2026, 10, 13), project_paths=["E:/work/project"], max_files=1)
    assert any("session file limit reached" in warning and "omitted 1" in warning
               for warning in result["warnings"])


def test_markdown_collection_warns_when_file_cap_truncates(tmp_path):
    for name in ("a.md", "b.md"):
        (tmp_path / name).write_text("---\ndate: 2026-10-13\nstatus: completed\n---\n完成本地校验。\n", encoding="utf-8")
    result = collect_markdown([tmp_path], "daily", date(2026, 10, 13), max_files=1)
    assert len(result["facts"]) == 1
    assert any("markdown file limit reached" in warning and "omitted 1" in warning
               for warning in result["warnings"])


def test_markdown_collection_warns_when_a_single_file_is_truncated(tmp_path):
    note = tmp_path / "large.md"
    note.write_text("---\ndate: 2026-10-13\nstatus: completed\n---\n" + "校验" * 6000, encoding="utf-8")
    result = collect_markdown([note], "daily", date(2026, 10, 13))
    assert len(result["facts"]) == 1
    assert any("markdown content limit reached" in warning and "large.md" in warning
               for warning in result["warnings"])


def test_git_collection_warns_when_commit_limit_is_reached(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import lablog.sources as sources

    rows = "\n".join(f"{i:040x}\t2026-10-13T01:00:00+00:00\tchange {i}" for i in range(200))
    def fake_run(command, **kwargs):
        if "log" in command:
            return SimpleNamespace(returncode=0, stdout=rows)
        return SimpleNamespace(returncode=0, stdout="")
    monkeypatch.setattr(sources.subprocess, "run", fake_run)
    result = sources.collect_git([tmp_path], "daily", date(2026, 10, 13))
    assert len(result["facts"]) == 200
    assert any("git commit limit reached" in warning for warning in result["warnings"])


def _auto_config(tmp_path, *, holidays=(), makeup=()):
    from lablog.workflow import atomic_json
    cal = tmp_path / "cal.json"
    cal.write_text(json.dumps({"year": 2026, "source": "official",
                               "holidays": list(holidays), "makeup_workdays": list(makeup)}), encoding="utf-8")
    return {"_base": str(tmp_path), "data_dir": "data", "calendar": {"file": "cal.json"},
            "dws": {"executable": "dws", "profile": "p"},
            "identity": {"corp_id": "corp", "user_id": "self", "corp_name": "lab", "user_name": "Self"},
            "recipients": [{"name": "Recipient", "user_id": "r", "corp_name": "lab"}],
            "templates": {"daily": {"id": "t", "name": "学生日报"}},
            "date_encoding": {"verified": True, "format": "iso-date"},
            "history_from": "2026-10-01",
            "notify": {"enabled": True, "from": "a@dingtalk.com", "to": ["operator@example.com"]}}


def test_auto_skips_non_workday_without_touching_dws_or_mail(tmp_path, monkeypatch):
    import lablog.cli as cli
    cfg = _auto_config(tmp_path, holidays=["2026-10-13"])
    sent = []
    monkeypatch.setattr(cli, "_client", lambda config: pytest.fail("DWS must not be touched"))
    monkeypatch.setattr(cli, "send_notification", lambda *a, **k: sent.append(a) or {"status": "sent"})
    result = cli._auto(cfg, "daily", "2026-10-13", "extractive", submit=True)
    assert result["status"] == "not_due"
    assert result["auto_submitted"] is False
    assert sent == []


def test_auto_mails_operator_and_never_submits_without_auth(tmp_path, monkeypatch):
    import lablog.cli as cli

    class FakeDws:
        def auth(self):
            raise DwsBlocked("DWS authentication unavailable")

    cfg = _auto_config(tmp_path)
    monkeypatch.setattr(cli, "_client", lambda config: FakeDws())
    sent = []
    monkeypatch.setattr(cli, "send_notification",
                        lambda _cfg, subject, body: sent.append((subject, body)) or {"status": "sent"})
    result = cli._auto(cfg, "daily", "2026-10-13", "extractive", submit=True)
    assert result["status"] == "blocked"
    assert result["auto_submitted"] is False
    assert len(sent) == 1 and "2026-10-13" in sent[0][0]
    assert "authentication" in sent[0][1]


def test_auto_stops_on_draft_blockers_before_dry_run(tmp_path, monkeypatch):
    import lablog.cli as cli
    from lablog.workflow import atomic_json

    class FakeDws:
        submits = 0
        def auth(self):
            return {"corp_id": "corp", "user_id": "self", "user_name": "Self"}
        def contact(self, ident):
            return {"orgUserName": "Recipient", "orgName": "lab"}
        def outbox(self, name, start, end):
            return []
        def submit(self, *args, **kwargs):
            self.submits += 1
            return {}

    cfg = _auto_config(tmp_path)
    draft = freeze_draft({"kind": "daily", "date": "2026-10-13", "template_id": "t",
                          "template_name": "学生日报", "template_fingerprint": "f", "fields": [],
                          "blockers": ["no dated completion evidence"]})
    path = tmp_path / "draft.json"
    atomic_json(path, draft)
    fake = FakeDws()
    monkeypatch.setattr(cli, "_client", lambda config: fake)
    monkeypatch.setattr(cli, "_generate", lambda *a, **k: (path, draft))
    monkeypatch.setattr(cli, "send_notification", lambda *a, **k: {"status": "sent"})
    result = cli._auto(cfg, "daily", "2026-10-13", "extractive", submit=True)
    assert result["status"] == "blocked"
    assert "no dated completion evidence" in result["reason"]
    assert fake.submits == 0


def test_auto_is_idempotent_after_a_verified_period(tmp_path, monkeypatch):
    import lablog.cli as cli
    from lablog.workflow import atomic_json
    cfg = _auto_config(tmp_path)
    draft = freeze_draft({"kind": "daily", "date": "2026-10-13", "template_id": "t",
                          "template_name": "学生日报", "template_fingerprint": "f", "fields": [],
                          "blockers": []})
    draft["submission"] = {"status": "verified", "report_id": "report-1"}
    path = tmp_path / "data" / "drafts" / "daily-2026-10-13.json"
    atomic_json(path, draft)
    sent = []
    monkeypatch.setattr(cli, "_client", lambda config: pytest.fail("must not touch DWS"))
    monkeypatch.setattr(cli, "send_notification", lambda *a, **k: sent.append(a) or {"status": "sent"})
    result = cli._auto(cfg, "daily", "2026-10-13", "extractive", submit=True)
    assert result["status"] == "already_submitted"
    assert result["report_id"] == "report-1"
    assert result["auto_submitted"] is False
    assert sent == []


def test_auto_stays_quiet_when_the_period_already_exists_remotely(tmp_path, monkeypatch):
    """A period that is already on the server is done, not a failure to report.

    Otherwise an every-workday schedule would mail the operator a false alarm
    each evening the report had already been filed by hand.
    """
    import lablog.cli as cli
    from lablog.workflow import atomic_json

    class FakeDws:
        submits = 0

        def auth(self):
            return {"corp_id": "corp", "user_id": "self", "user_name": "Self"}

        def contact(self, ident):
            return {"orgUserName": "Recipient", "orgName": "lab"}

        def outbox(self, name, start, end):
            return [{"reportId": "existing", "createTime": 1}]

        def detail(self, report_id):
            return {"report_content": [{"key": "今日完成",
                                        "value": "统计周期：2026-10-13 至 2026-10-13\n"}]}

        def submit(self, *args, **kwargs):
            self.submits += 1
            return {}

    cfg = _auto_config(tmp_path)
    draft = freeze_draft({"kind": "daily", "date": "2026-10-13", "template_id": "t",
                          "template_name": "学生日报", "template_fingerprint": "f", "fields": [],
                          "blockers": []})
    path = tmp_path / "draft.json"
    atomic_json(path, draft)
    fake = FakeDws()
    monkeypatch.setattr(cli, "_client", lambda config: fake)
    monkeypatch.setattr(cli, "_generate", lambda *a, **k: (path, draft))
    sent = []
    monkeypatch.setattr(cli, "send_notification", lambda *a, **k: sent.append(a) or {"status": "sent"})
    result = cli._auto(cfg, "daily", "2026-10-13", "extractive", submit=True)
    assert result["status"] == "already_submitted"
    assert result["auto_submitted"] is False
    assert fake.submits == 0
    assert sent == []


def test_scope_file_overrides_only_the_keys_it_names(tmp_path):
    import lablog.cli as cli
    cfg = {"codex": {"mode": "auto_work", "project_paths": ["E:/old"], "session_ids": ["s1"]},
           "supplemental": {"git_paths": ["E:/git"], "markdown_paths": ["E:/md"]}}
    scope = tmp_path / "scope.json"
    scope.write_text(json.dumps({"project_paths": ["E:/project/VR"]}), encoding="utf-8")
    scoped = cli._apply_scope(cfg, str(scope))
    assert scoped["codex"]["project_paths"] == ["E:/project/VR"]
    assert scoped["codex"]["mode"] == "selected"
    assert scoped["codex"]["session_ids"] == ["s1"]
    assert scoped["supplemental"]["git_paths"] == ["E:/git"]
    assert scoped["supplemental"]["markdown_paths"] == ["E:/md"]
    assert cfg["codex"]["project_paths"] == ["E:/old"]
    assert scoped["scope_source"].endswith("scope.json")


def test_scope_file_cannot_remove_configured_exclusions(tmp_path):
    import lablog.cli as cli
    cfg = {"codex": {"exclude_paths": ["E:/project/galgame news", "E:/project/Comfyui"]},
           "supplemental": {}}
    scope = tmp_path / "scope.json"
    scope.write_text(json.dumps({"exclude_paths": []}), encoding="utf-8")

    scoped = cli._apply_scope(cfg, str(scope))

    assert scoped["codex"]["exclude_paths"] == cfg["codex"]["exclude_paths"]


def test_excluded_directories_block_explicit_and_parent_supplemental_paths():
    from lablog.cli import _filter_excluded_paths

    paths = ["E:/project/galgame news", "E:/project", "E:/project/VR"]
    assert _filter_excluded_paths(paths, ["e:\\PROJECT\\GalGame News\\"]) == ["E:/project/VR"]


def test_collection_applies_exclusions_to_supplemental_sources(tmp_path, monkeypatch):
    import lablog.cli as cli
    seen = {}

    def capture(key, paths):
        seen[key] = [str(x) for x in paths]
        return {"facts": [], "warnings": []}

    monkeypatch.setattr(cli, "collect_codex", lambda *a, **k: {"facts": [], "warnings": []})
    monkeypatch.setattr(cli, "collect_git", lambda paths, *a: capture("git", paths))
    monkeypatch.setattr(cli, "collect_markdown", lambda paths, *a: capture("markdown", paths))
    cfg = {
        "data_dir": str(tmp_path),
        "codex": {"mode": "auto_work", "session_roots": [], "exclude_paths": ["E:/project/galgame news"]},
        "supplemental": {"git_paths": ["E:/project/galgame news", "E:/project", "E:/project/VR"],
                         "markdown_paths": ["E:/project/galgame news", "E:/project", "E:/project/VR"]},
    }

    _, result = cli._collect(cfg, "daily", "2026-10-23", persist=False)

    assert seen == {"git": ["E:\\project\\VR"], "markdown": ["E:\\project\\VR"]}
    assert result["sources"][1]["scanned_paths"] == ["E:\\project\\VR"]


def test_retained_evidence_drops_facts_from_excluded_roots(tmp_path, monkeypatch):
    import lablog.cli as cli
    from lablog.workflow import atomic_json
    cfg = {"data_dir": str(tmp_path),
           "codex": {"mode": "auto_work", "session_roots": [str(tmp_path / "missing-sessions")],
                     "exclude_paths": ["E:/project/galgame news", "E:/project/Comfyui"]},
           "supplemental": {}}
    path = cli._path(cfg, "daily", "2026-10-23", "evidence")
    atomic_json(path, {"kind": "daily", "date": "2026-10-23", "facts": [
        {"evidence_id": "galgame", "project": "E:/project/galgame news", "text": "excluded old fact"},
        {"evidence_id": "comfy", "source_path": "E:/project/Comfyui/work.md", "text": "excluded old file"},
        {"evidence_id": "vr", "project": "E:/project/VR", "text": "allowed old fact"}],
        "source_fact_count": 3, "deduped_fact_count": 3, "sources": []})
    monkeypatch.setattr(cli, "collect_codex", lambda *a, **k: {"facts": [], "warnings": []})

    _, result = cli._collect(cfg, "daily", "2026-10-23")

    assert result["retention"]["action"] == "retain"
    assert [fact["evidence_id"] for fact in result["facts"]] == ["vr"]


def test_scope_file_rejects_unknown_keys_and_bad_types(tmp_path):
    import lablog.cli as cli
    cfg = {"codex": {}, "supplemental": {}}
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"project_path": ["E:/x"]}), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown scope keys"):
        cli._apply_scope(cfg, str(bad))
    bad.write_text(json.dumps({"project_paths": "E:/x"}), encoding="utf-8")
    with pytest.raises(ValueError, match="list of strings"):
        cli._apply_scope(cfg, str(bad))
    bad.write_text(json.dumps(["E:/x"]), encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        cli._apply_scope(cfg, str(bad))
    assert cli._apply_scope(cfg, None) is cfg


def test_auto_records_the_scope_source_in_its_outcome(tmp_path, monkeypatch):
    import lablog.cli as cli
    cfg = _auto_config(tmp_path)
    cfg["codex"] = {"mode": "auto_work", "project_paths": []}
    cfg["supplemental"] = {}
    scope = tmp_path / "scope.json"
    scope.write_text(json.dumps({"project_paths": ["E:/project/VR"]}), encoding="utf-8")
    monkeypatch.setattr(cli, "_client", lambda config: pytest.fail("non-workday gate must run first"))
    result = cli._auto(cfg, "daily", "2026-10-11", "extractive", scope_file=str(scope))
    assert result["status"] == "not_due"
    assert result["scope_source"].endswith("scope.json")


def test_notify_is_disabled_by_default_and_targets_every_recipient(monkeypatch):
    from lablog import notify as notify_module
    cfg = {"dws": {"executable": "dws", "profile": "p"},
           "notify": {"enabled": False, "from": "a@dingtalk.com", "to": ["operator@example.com"]}}
    assert notify_module.send(cfg, "s", "b")["status"] == "disabled"

    seen = {}

    class Proc:
        returncode = 0
        stdout = '{"success": "true"}'
        stderr = ""

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return Proc()

    monkeypatch.setattr(notify_module.subprocess, "run", fake_run)
    cfg["notify"]["enabled"] = True
    cfg["notify"]["to"] = ["operator@example.com", "1qg-0pnyng6neq@dingtalk.com"]
    assert notify_module.send(cfg, "s", "b")["status"] == "sent"
    argv = seen["argv"]
    assert argv[-1] == "b"
    assert "operator@example.com,1qg-0pnyng6neq@dingtalk.com" in argv
    assert "mail" in argv and "send" in argv


# ---------------------------------------------------------------------------
# Carryover backlog: staging, theme-complete trimming and backfill
# ---------------------------------------------------------------------------

def _carryover_cfg(tmp_path, *, enabled=True, target=200, plan_extra=True, apply_to=("completed",)):
    """A minimal cfg plus the carryover block, mirroring load_config defaults."""
    return {"_base": str(tmp_path), "data_dir": "data",
            "quality": {"carryover": {"enabled": enabled, "target_chars": target,
                                      "apply_to": list(apply_to), "plan_extra": plan_extra}}}


def _daily_draft(marker, body, *, plan=None, citations=None):
    fields = [{"name": "今日完成", "type": 1, "sort": 0, "value": marker + "\n" + body}]
    if plan is not None:
        fields.append({"name": "明日计划", "type": 1, "sort": 2, "value": plan})
    return {"kind": "daily", "date": "2026-10-13", "fields": fields, "blockers": [],
            "warnings": [], "citations": citations or {}, "generation_engine": "extractive"}


def test_backlog_append_is_idempotent_by_content(tmp_path):
    from lablog import backlog
    cfg = _carryover_cfg(tmp_path)
    item = {"text": "完成了解析器重构。", "created_date": "2026-10-13",
            "source": "codex-assisted", "evidence_ids": ["e1"]}
    assert backlog.append(cfg, [item]) == 1
    assert backlog.append(cfg, [dict(item)]) == 0
    assert len(backlog.load(cfg)["items"]) == 1


def test_backlog_take_is_fifo_truncating_and_reports_remaining(tmp_path):
    from lablog import backlog
    cfg = _carryover_cfg(tmp_path)
    backlog.append(cfg, [
        {"text": "aaaa", "created_date": "2026-10-10"},
        {"text": "bbbb", "created_date": "2026-10-11"},
        {"text": "ccccc", "created_date": "2026-10-12"},
    ])
    selected, total, remaining = backlog.take(cfg, 100)
    assert [x["text"] for x in selected] == ["aaaa", "bbbb", "ccccc"]  # FIFO
    assert total == 13 and remaining == 0

    selected, total, remaining = backlog.take(cfg, 4)
    assert [x["text"] for x in selected] == ["aaaa"]  # truncates to the limit
    assert total == 4 and remaining == 2

    # A single item larger than the limit is still returned (at least one).
    selected, _, _ = backlog.take(cfg, 1)
    assert [x["text"] for x in selected] == ["aaaa"]

    # The remaining count is measured after applying the date filter.
    selected, _, remaining = backlog.take(cfg, 100, date="2026-10-11")
    assert [x["text"] for x in selected] == ["aaaa", "bbbb"]
    assert remaining == 0


def test_backlog_mark_consumed_is_idempotent_and_stats_track_chars(tmp_path):
    from lablog import backlog
    cfg = _carryover_cfg(tmp_path)
    backlog.append(cfg, [
        {"text": "已完成第一项工作。", "created_date": "2026-10-10"},
        {"text": "已完成第二项工作内容。", "created_date": "2026-10-11"},
    ])
    before = backlog.stats(cfg)
    assert before["pending_items"] == 2
    assert before["pending_chars"] == backlog.char_count("已完成第一项工作。") + backlog.char_count("已完成第二项工作内容。")

    selected, _, _ = backlog.take(cfg, 100)
    ids = [x["id"] for x in selected]
    assert backlog.mark_consumed(cfg, ids, "2026-10-13", "report-9") == 2
    assert backlog.mark_consumed(cfg, ids, "2026-10-13", "report-9") == 0  # idempotent

    after = backlog.stats(cfg)
    assert after["pending_items"] == 0 and after["pending_chars"] == 0
    assert after["consumed_items"] == 2


def test_carryover_disabled_leaves_body_untouched_and_draft_free_of_field(tmp_path):
    from lablog import backlog
    from lablog.cli import _apply_carryover
    from lablog.periods import format_period_marker
    cfg = _carryover_cfg(tmp_path, enabled=False)
    body = "".join(f"项目VR的第{i}项工作已完成并通过自测。" for i in range(1, 21))
    draft = _daily_draft(format_period_marker("daily", "2026-10-13"), body)
    evidence = {"facts": [{"source": "codex", "project": r"E:\project\VR", "evidence_id": "v1"}]}
    snapshot = json.dumps(draft, ensure_ascii=False, sort_keys=True)
    assert _apply_carryover(cfg, "daily", "2026-10-13", draft, evidence) == {"status": "disabled"}
    assert json.dumps(draft, ensure_ascii=False, sort_keys=True) == snapshot
    assert "carryover" not in draft
    assert not backlog._path(cfg).exists()


def test_carryover_trims_above_target_and_queues_overflow(tmp_path):
    from lablog import backlog
    from lablog.cli import _apply_carryover
    from lablog.periods import format_period_marker
    cfg = _carryover_cfg(tmp_path, target=200)
    # Five themes so whole trailing blocks can be staged while still meeting the
    # lower bound from the blocks that remain.
    names = ["VR", "钉钉", "Other", "Alpha", "Beta"]
    body = "".join(
        f"项目{name}的第{i}项工作已完成并通过了本地自测。"
        for name in names for i in range(1, 6))
    marker = format_period_marker("daily", "2026-10-13")
    draft = _daily_draft(marker, body, citations={"今日完成": ["e1"]})
    evidence = {"facts": [{"source": "codex", "project": str(tmp_path / name), "evidence_id": name}
                          for name in names]}
    result = _apply_carryover(cfg, "daily", "2026-10-13", draft, evidence)
    assert result["status"] == "applied"
    kept_value = draft["fields"][0]["value"]
    assert kept_value.startswith(marker + "\n")  # period marker is not part of the budget
    kept_body = kept_value.split("\n", 1)[1]
    assert backlog.char_count(kept_body) >= 200  # meets the lower bound
    assert kept_body.endswith("。")  # cut on a sentence boundary

    items = backlog.load(cfg)["items"]
    assert items and all(x["kind"] == "completed" for x in items)
    assert result["target_chars"] == 200
    assert result["kept_chars"] == backlog.char_count(kept_body)
    assert result["queued_items"] and result["queued_chars"] == sum(backlog.char_count(x["text"]) for x in items)
    assert draft["carryover"]["target_chars"] == 200
    assert draft["carryover"]["queued_items"] == result["queued_items"]
    assert json.loads(json.dumps(draft["carryover"])) == draft["carryover"]


def test_carryover_keeps_whole_theme_blocks_and_tags_plan(tmp_path):
    from lablog import backlog
    from lablog.cli import _apply_carryover
    from lablog.periods import format_period_marker
    cfg = _carryover_cfg(tmp_path, target=120, plan_extra=True)
    vr = "".join(f"项目VR的第{i}项联调工作已完成。" for i in range(1, 5))
    md = "".join(f"项目Markdown的第{i}项文档工作已完成。" for i in range(1, 5))
    marker = format_period_marker("daily", "2026-10-13")
    plan_text = "明天准备推进第二批界面接入与联调。"
    draft = _daily_draft(marker, vr + md, plan=plan_text)
    # markdown theme is derived from the note's parent directory.
    evidence = {"facts": [
        {"source": "codex", "project": r"E:\project\VR", "evidence_id": "v1"},
        {"source": "markdown", "path": str(tmp_path / "notes" / "2026-10-13.md"), "evidence_id": "m1"}]}
    _apply_carryover(cfg, "daily", "2026-10-13", draft, evidence)
    items = backlog.load(cfg)["items"]
    plan_items = [x for x in items if x["kind"] == "plan"]
    completed_items = [x for x in items if x["kind"] == "completed"]
    assert [x["text"] for x in plan_items] == [plan_text]
    # Each queued completed item is a run of whole sentences, never a fragment.
    for entry in completed_items:
        for sentence in entry["text"].split("\n"):
            assert sentence.endswith("。")
    kept_body = draft["fields"][0]["value"].split("\n", 1)[1]
    for sentence in kept_body.split("\n"):
        assert sentence.endswith("。")


def test_fill_from_backlog_uses_completed_only_and_keeps_blocker_when_empty(tmp_path):
    from lablog import backlog
    from lablog.cli import _fill_from_backlog
    from lablog.periods import format_period_marker
    # Completed present -> filled, blocker removed.
    cfg = _carryover_cfg(tmp_path)
    backlog.append(cfg, [
        {"text": "十月遗留的已完成工作总结。", "created_date": "2026-10-08", "kind": "completed"},
        {"text": "明天准备推进第二批界面接入。", "created_date": "2026-10-08", "kind": "plan"},
    ])
    draft = _daily_draft(format_period_marker("daily", "2026-10-20"), "")
    draft["blockers"] = ["no dated completion evidence"]
    result = _fill_from_backlog(cfg, "daily", "2026-10-20", draft, {"facts": []})
    assert result["status"] == "filled"
    body = draft["fields"][0]["value"].split("\n", 1)[1]
    assert "十月遗留的已完成工作总结。" in body
    assert "明天准备推进" not in body  # plan text must never stand in for completed work
    assert "no dated completion evidence" not in draft["blockers"]
    assert draft["carryover"]["filled_items"]

    # Empty backlog -> skipped, blocker preserved.
    empty = _carryover_cfg(tmp_path / "empty")
    draft_empty = _daily_draft(format_period_marker("daily", "2026-10-20"), "")
    draft_empty["blockers"] = ["no dated completion evidence"]
    skipped = _fill_from_backlog(empty, "daily", "2026-10-20", draft_empty, {"facts": []})
    assert skipped["status"] == "skipped"
    assert "no dated completion evidence" in draft_empty["blockers"]

    # Only plan items -> skipped, blocker preserved.
    plan_only = _carryover_cfg(tmp_path / "plan-only")
    backlog.append(plan_only, [{"text": "明天准备推进第二批。", "created_date": "2026-10-08", "kind": "plan"}])
    draft_plan = _daily_draft(format_period_marker("daily", "2026-10-20"), "")
    draft_plan["blockers"] = ["no dated completion evidence"]
    plan_skipped = _fill_from_backlog(plan_only, "daily", "2026-10-20", draft_plan, {"facts": []})
    assert plan_skipped["status"] == "skipped"
    assert "no dated completion evidence" in draft_plan["blockers"]


def test_backfilled_body_has_no_traceability_markers(tmp_path):
    import re
    from lablog import backlog
    from lablog.cli import _fill_from_backlog
    from lablog.periods import format_period_marker
    cfg = _carryover_cfg(tmp_path)
    # A legacy-looking item still carrying an inline citation must be cleaned.
    backlog.append(cfg, [{"text": "完成了解析器修复 [证据:abc123]。", "created_date": "2026-10-08"}])
    draft = _daily_draft(format_period_marker("daily", "2026-10-20"), "")
    draft["blockers"] = ["no dated completion evidence"]
    _fill_from_backlog(cfg, "daily", "2026-10-20", draft, {"facts": []})
    body = draft["fields"][0]["value"]
    assert not re.search(r"\[证据:", body)


def test_submit_consumes_backlog_only_after_verified(tmp_path, monkeypatch):
    from lablog import backlog
    from lablog.workflow import atomic_json
    cfg, path, draft, fake = _ready_submission(tmp_path, monkeypatch)
    backlog.append(cfg, [{"text": "待补交的完成内容。", "created_date": "2026-10-08"}])
    item_id = backlog.load(cfg)["items"][0]["id"]
    draft["carryover"] = {"filled_items": [item_id], "filled_chars": 9}
    atomic_json(path, freeze_draft(draft))
    signed = json.loads(path.read_text(encoding="utf-8"))
    result = _submit(cfg, path, signed["hash"])
    assert result["status"] == "verified"
    assert result["backlog_consumed"] == 1
    entry = backlog.load(cfg)["items"][0]
    assert entry["status"] == "consumed"
    assert entry["consumed_report_id"] == "report-1"


def test_submit_failure_never_consumes_backlog(tmp_path, monkeypatch):
    from lablog import backlog
    from lablog.workflow import atomic_json
    cfg, path, draft, fake = _ready_submission(tmp_path, monkeypatch, outcome="timeout")
    backlog.append(cfg, [{"text": "待补交的完成内容。", "created_date": "2026-10-08"}])
    item_id = backlog.load(cfg)["items"][0]["id"]
    draft["carryover"] = {"filled_items": [item_id], "filled_chars": 9}
    atomic_json(path, freeze_draft(draft))
    signed = json.loads(path.read_text(encoding="utf-8"))
    with pytest.raises(DwsBlocked):
        _submit(cfg, path, signed["hash"])
    assert backlog.load(cfg)["items"][0]["status"] == "pending"
    assert backlog.load(cfg)["items"][0]["consumed_report_id"] is None


def test_draft_hash_is_self_consistent_with_carryover(tmp_path):
    from lablog.cli import _apply_carryover
    from lablog.periods import format_period_marker
    from lablog.workflow import draft_hash
    cfg = _carryover_cfg(tmp_path, target=200)
    body = "".join(f"项目VR的第{i}项联调工作已完成并通过了本地自测。" for i in range(1, 5))
    body += "".join(f"钉钉项目的第{i}项接入工作已完成并完成验证。" for i in range(1, 5))
    draft = _daily_draft(format_period_marker("daily", "2026-10-13"), body)
    evidence = {"facts": [{"source": "codex", "project": r"E:\project\VR", "evidence_id": "v1"},
                          {"source": "codex", "project": r"E:\project\钉钉", "evidence_id": "d1"}]}
    _apply_carryover(cfg, "daily", "2026-10-13", draft, evidence)
    draft["hash"] = draft_hash(draft)
    assert draft["hash"] == draft_hash(draft)


def test_auto_submits_valid_manual_saved_draft_without_regenerating(tmp_path, monkeypatch):
    import lablog.cli as cli
    from lablog.workflow import atomic_json

    cfg = _auto_config(tmp_path)
    template = {"report_template_id": "t", "report_template_name": "学生日报",
                "allowAddReceivers": 1,
                "report_template_fields": [{"field_name": "今日完成", "field_sort": 0,
                                             "field_type": 1}]}
    manual_body = "人工保存的正文：已完成页面联调并核对回归结果。"
    draft = freeze_draft({"kind": "daily", "date": "2026-10-13", "template_id": "t",
                          "template_name": "学生日报", "template_fingerprint": template_fingerprint(template),
                          "fields": [{"name": "今日完成", "type": 1, "sort": 0, "value": manual_body}],
                          "blockers": [], "warnings": [], "manual_saved": True,
                          "approval_context": _approval_context(cfg, "daily", "2026-10-13")})
    path = tmp_path / "data" / "drafts" / "daily-2026-10-13.json"
    atomic_json(path, draft)

    class FakeDws:
        calls = []

        def auth(self):
            return {"corp_id": "corp", "user_id": "self", "user_name": "Self"}

        def template(self, name, ident):
            assert (name, ident) == ("学生日报", "t")
            return template

        def contact(self, ident):
            assert ident == "r"
            return {"orgUserName": "Recipient", "orgName": "lab"}

        def outbox(self, name, start, end):
            assert name == "学生日报"
            return []

        def submit(self, ident, contents, users, **kwargs):
            self.calls.append((ident, contents, users, kwargs))
            return {"status": "dry_run"} if kwargs.get("dry_run") else "manual-report"

        def detail(self, ident):
            assert ident == "manual-report"
            contents = next(call[1] for call in reversed(self.calls) if not call[3].get("dry_run"))
            return {"report_template_id": "t", "reportId": ident, "contents": contents}

    fake = FakeDws()
    monkeypatch.setattr(cli, "_client", lambda config: fake)
    monkeypatch.setattr(cli, "_generate", lambda *a, **k: pytest.fail("manual-saved draft must not be regenerated"))
    result = cli._auto(cfg, "daily", "2026-10-13", "extractive", submit=True)

    assert result["status"] == "verified"
    assert result["auto_submitted"] is True
    assert [step["step"] for step in result["steps"] if step["step"] in
            {"auth", "recipients", "generate", "preview", "dry_run", "submit"}] == [
                "auth", "recipients", "generate", "preview", "dry_run", "submit"]
    dry_run, actual = fake.calls
    assert dry_run[3] == {"dry_run": True}
    assert actual[3] == {}
    assert actual[1][0]["content"] == manual_body
    assert json.loads(path.read_text(encoding="utf-8"))["submission"]["status"] == "verified"


def test_generate_preserves_manual_saved_draft_if_saved_during_collection(tmp_path, monkeypatch):
    import lablog.cli as cli
    from lablog.workflow import atomic_json

    cfg = _auto_config(tmp_path)
    template = {"report_template_id": "t", "report_template_name": "学生日报",
                "allowAddReceivers": 1,
                "report_template_fields": [{"field_name": "今日完成", "field_sort": 0,
                                             "field_type": 1}]}
    manual_body = "网页刚保存的正文，应优先保留。"
    prior = freeze_draft({"kind": "daily", "date": "2026-10-13", "template_id": "t",
                          "template_name": "学生日报", "template_fingerprint": template_fingerprint(template),
                          "fields": [{"name": "今日完成", "type": 1, "sort": 0, "value": manual_body}],
                          "blockers": [], "warnings": [], "manual_saved": True,
                          "approval_context": _approval_context(cfg, "daily", "2026-10-13")})
    path = tmp_path / "data" / "drafts" / "daily-2026-10-13.json"
    atomic_json(path, prior)
    before = path.read_bytes()

    class FakeDws:
        def template(self, name, ident):
            return template

    monkeypatch.setattr(cli, "_client", lambda config: FakeDws())
    monkeypatch.setattr(cli, "_collect", lambda *args: (tmp_path / "evidence.json", {
        "facts": [{"evidence_id": "new", "status": "completed", "text": "newly collected work"}],
        "warnings": []}))

    returned_path, returned, info = cli._generate_impl(cfg, "daily", "2026-10-13", "extractive")

    assert returned_path == path
    assert returned == prior
    assert info["action"] == "preserved_manual"
    assert path.read_bytes() == before
    assert json.loads(path.read_text(encoding="utf-8"))["fields"][0]["value"] == manual_body


