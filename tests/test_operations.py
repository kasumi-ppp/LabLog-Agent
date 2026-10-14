"""Heartbeats, alerts, schedule gap detection and local diagnostics (P1/P2)."""

import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from lablog import cli as cli_module
from lablog import doctor, state
from lablog.dws import template_fingerprint
from lablog.workflow import atomic_json, freeze_draft

DAY = "2026-10-14"


def _template():
    return {"report_template_id": "t", "report_template_name": "学生日报", "allowAddReceivers": 1,
            "lastModifiedTime": 1,
            "report_template_fields": [{"field_name": "今日完成", "field_sort": 0, "field_type": 1}]}


class FakeDws:
    def __init__(self):
        self.submits = 0

    def auth(self):
        return {"corp_id": "corp", "user_id": "self", "user_name": "Self"}

    def template(self, name, ident):
        return _template()

    def contact(self, ident):
        return {"orgUserName": "Recipient", "orgName": "lab"}

    def outbox(self, name, start, end):
        return []

    def submit(self, ident, contents, users, dry_run=False):
        self.submits += 1
        return "report-1"

    def detail(self, ident):
        return {"report_template_id": "t", "reportId": ident,
                "report_content": [{"key": "今日完成", "value": "Done"}]}


def auto_cfg(tmp_path):
    cal = tmp_path / "cal.json"
    cal.write_text(json.dumps({"year": 2026, "source": "official", "holidays": [], "makeup_workdays": []}),
                   encoding="utf-8")
    return {"_base": str(tmp_path), "data_dir": "data", "calendar": {"file": "cal.json"},
            "dws": {"executable": "dws", "profile": "p"},
            "identity": {"corp_id": "corp", "user_id": "self", "corp_name": "lab", "user_name": "Self"},
            "recipients": [{"name": "Recipient", "user_id": "r", "corp_name": "lab"}],
            "templates": {"daily": {"id": "t", "name": "学生日报"}},
            "date_encoding": {"verified": True, "format": "iso-date"},
            "history_from": "2026-10-01",
            "notify": {"enabled": True, "from": "a@dingtalk.com", "to": ["operator@example.com"]}}


def auto_draft(cfg):
    return freeze_draft({"kind": "daily", "date": DAY, "template_id": "t", "template_name": "学生日报",
                         "template_fingerprint": template_fingerprint(_template()),
                         "fields": [{"name": "今日完成", "type": 1, "sort": 0, "value": "Done"}],
                         "blockers": [], "approval_context": cli_module._approval_context(cfg, "daily", DAY)})


# --------------------------------------------------------------------------
# P1-14 heartbeats
# --------------------------------------------------------------------------

def test_auto_records_a_heartbeat_for_every_run(tmp_path, monkeypatch):
    cfg = auto_cfg(tmp_path)
    draft = auto_draft(cfg)
    path = tmp_path / "draft.json"
    atomic_json(path, draft)
    fake = FakeDws()
    monkeypatch.setattr(cli_module, "_client", lambda _: fake)
    monkeypatch.setattr(cli_module, "_generate", lambda *a, **k: (path, draft))
    outcome = cli_module._auto(cfg, "daily", DAY, "extractive", submit=True)
    assert outcome["status"] == "verified"
    beats = state.read_heartbeats(cfg)
    assert beats and beats[0]["task"] == "auto-daily"
    assert beats[0]["outcome"] == "verified"
    assert beats[0]["run_id"] == outcome["run_id"]


def test_missing_runs_are_detected_from_the_heartbeat(tmp_path, monkeypatch):
    cfg = auto_cfg(tmp_path)
    report = doctor.missed_runs(cfg, days=7)
    assert report["missing"], "a day with no run at all must be reported"
    state.heartbeat_finish(cfg, "auto-daily", "verified", run_id="manual")
    report = doctor.missed_runs(cfg, days=7)
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    assert today not in report["missing"], "a completed run closes today's gap"
    assert report["missing"], "earlier days with no run stay reported"


# --------------------------------------------------------------------------
# P1-14 alerts survive a broken mail channel
# --------------------------------------------------------------------------

def test_undelivered_alert_is_kept_and_can_be_resent(tmp_path, monkeypatch):
    cfg = auto_cfg(tmp_path)
    sent = []
    monkeypatch.setattr(cli_module, "send_notification",
                        lambda *a, **k: sent.append(a) or {"status": "failed", "reason": "token"})
    outcome = {"kind": "daily", "date": DAY, "status": "unknown", "reason": "auth failed", "steps": []}
    cli_module._notify_outcome(cfg, outcome)
    assert outcome["alert_delivered"] is False
    pending = state.pending_alerts(cfg)
    assert len(pending) == 1, "an alert that could not be mailed must stay on disk"
    assert "dws auth login" in pending[0].read_text(encoding="utf-8")
    assert doctor.collect(cfg)["pending_alerts"]
    monkeypatch.setattr("lablog.notify.send", lambda *a, **k: {"status": "sent"})
    resent = doctor.resend_alerts(cfg, cli_module.send_notification)
    assert resent["delivered"] and not resent["failed"]
    assert state.pending_alerts(cfg) == []


def test_delivered_alert_is_cleared(tmp_path, monkeypatch):
    cfg = auto_cfg(tmp_path)
    monkeypatch.setattr(cli_module, "send_notification", lambda *a, **k: {"status": "sent"})
    outcome = {"kind": "daily", "date": DAY, "status": "unknown", "reason": "boom", "steps": []}
    cli_module._notify_outcome(cfg, outcome)
    assert outcome["alert_delivered"] is True
    assert state.pending_alerts(cfg) == []


def test_diagnostics_keep_every_failure_separately(tmp_path, monkeypatch):
    """Repeated failures in one day must not overwrite each other's evidence."""
    cfg = auto_cfg(tmp_path)
    for index, (status, reason) in enumerate([("blocked", "auth failed"), ("error", "boom"),
                                              ("unknown", "timeout")]):
        monkeypatch.setattr(cli_module, "send_notification", lambda *a, **k: {"status": "failed"})
        cli_module._notify_outcome(cfg, {"kind": "daily", "date": DAY, "status": status,
                                         "reason": reason, "steps": []})
    directory = tmp_path / "data" / "diagnostics" / "alerts"
    names = sorted(p.name for p in directory.glob("*.md"))
    assert len(names) == 3, names
    assert len(set(names)) == 3, "each failure keeps its own record"


# --------------------------------------------------------------------------
# P2-16 local diagnostics
# --------------------------------------------------------------------------

def test_doctor_reports_stale_locks_temps_and_unresolved_periods(tmp_path, monkeypatch):
    cfg = auto_cfg(tmp_path)
    draft = auto_draft(cfg)
    path = tmp_path / "data" / "drafts" / f"daily-{DAY}.json"
    atomic_json(path, draft)
    state_path = cli_module._state_path(cfg, draft)
    state.write_attempt(state_path, status="unknown", phase=state.PHASE_REQUEST_PENDING,
                        draft_path=str(path), draft_hash=draft["hash"], template_id="t")
    stale_lock = path.with_name(path.name + ".lock")
    stale_lock.write_text(json.dumps({"schema": 1, "pid": 999997, "host": socket.gethostname(),
                                      "token": "dead"}), encoding="utf-8")
    temp = tmp_path / "data" / "drafts" / "daily-2026-10-14.json.999997.tmp"
    temp.write_text("x", encoding="utf-8")
    old = time.time() - 7200
    os.utime(temp, (old, old))
    report = doctor.collect(cfg)
    assert report["data_dir"] == str(tmp_path / "data")
    assert report["locks"] and report["locks"][0]["owner_alive"] is False
    assert report["temp_files"]
    assert report["needs_manual_review"], "an unresolved period must be surfaced"
    assert any("lock" in issue for issue in report["issues"])
    assert report["counts"]["drafts"] == 1


def test_doctor_flags_an_unwritable_data_directory(tmp_path):
    cfg = {"_base": str(tmp_path), "data_dir": str(Path("Z:/definitely-missing/lablog"))}
    report = doctor.collect(cfg)
    assert report["data_dir_writable"] is False
    assert any("not writable" in issue for issue in report["issues"])


# --------------------------------------------------------------------------
# P1-12 configuration resolution
# --------------------------------------------------------------------------

def test_relative_config_resolves_against_the_project_root():
    root = Path(cli_module.PROJECT_ROOT)
    assert cli_module._config_path(None) == root / "config" / "config.local.yaml"
    assert cli_module._config_path("config/config.auto.yaml") == root / "config" / "config.auto.yaml"
    absolute = Path("D:/elsewhere/cfg.yaml")
    assert cli_module._config_path(str(absolute)) == absolute


def test_every_command_reports_the_directory_it_used(tmp_path, monkeypatch, capsys):
    """A run launched from anywhere must say where its data actually lives."""
    cfg = auto_cfg(tmp_path)
    config_file = tmp_path / "cfg.yaml"
    config_file.write_text(json.dumps(cfg), encoding="utf-8")  # JSON is valid YAML
    monkeypatch.setattr(cli_module, "_collect",
                        lambda *a, **k: (tmp_path / "e.json", {"facts": [], "warnings": []}))
    code = cli_module.main(["--config", str(config_file), "collect", "--kind", "daily", "--date", DAY])
    assert code == 0
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert printed["data_dir"] == str(tmp_path / "data")
    assert printed["config"] == str(config_file)
