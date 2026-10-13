import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from lablog.sources import collect_codex, collect_markdown, period_bounds
from lablog.workflow import freeze_draft, validate_submission, SubmissionBlocked
from lablog.dws import template_fingerprint, encode_contents, DwsBlocked, DwsClient
from lablog.report import build_draft
from lablog.cli import _approval_context, _deduplicate_facts, _outbox_history, _preview_markdown, _readback_matches, _report_period, _submit, _reconcile, _verify_recipients
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


def _ready_submission(tmp_path, monkeypatch, *, outcome="verified"):
    import lablog.cli as cli
    from lablog.workflow import atomic_json
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    template = {"report_template_id": "t", "report_template_name": "学生日报", "allowAddReceivers": 1,
                "report_template_fields": [{"field_name": "今日完成", "field_sort": 0, "field_type": 1}]}
    cfg = {"_base": str(tmp_path), "data_dir": "data", "dws": {"profile": "p"},
           "identity": {"corp_id": "corp", "user_id": "self", "corp_name": "lab", "user_name": "Self"},
           "recipients": [{"name": "Recipient", "user_id": "r", "corp_name": "lab"}],
           "templates": {"daily": {"id": "t", "name": "学生日报"}}, "history_from": today}
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
                return {"report_template_id": "t", "work_period": {"start": "2026-10-13", "end": "2026-10-13"}}
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


def test_possible_remote_duplicate_blocks_before_submit(tmp_path, monkeypatch):
    cfg, path, draft, fake = _ready_submission(tmp_path, monkeypatch)
    fake.outbox = lambda *a: [{"reportId": "existing"}]
    with pytest.raises(SubmissionBlocked, match="remote report"):
        _submit(cfg, path, draft["hash"])
    assert fake.submits == 0


def test_old_report_in_another_period_does_not_block_new_period(tmp_path, monkeypatch):
    cfg, path, draft, fake = _ready_submission(tmp_path, monkeypatch)
    fake.outbox = lambda *a: [{"reportId": "existing"}]
    fake_detail = fake.detail
    fake.detail = lambda ident: ({"report_template_id": "t", "work_period": {"start": "2026-10-12", "end": "2026-10-12"}}
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
