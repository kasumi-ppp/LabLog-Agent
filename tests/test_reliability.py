"""Persistence reliability: locks, atomic writes, crash recovery, transactions.

Cross-process tests spawn a clean interpreter with ``subprocess`` (Windows has
no fork, so ``monkeypatch`` state would not survive a forked child anyway) and
synchronise through gate files instead of sleeping.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lablog import state
from lablog.dws import DwsBlocked, template_fingerprint
from lablog.workflow import (CorruptDocument, LockBusy, SubmissionBlocked, atomic_json, atomic_text,
                             diagnostics_dir, file_lock, freeze_draft, load_json_durable, lock_path_for,
                             multi_lock, process_alive, read_lock_owner, release_lock, sweep_temp_files)

ROOT = str(Path(__file__).resolve().parents[1])
DAY = "2026-10-14"


def _template():
    return {"report_template_id": "t", "report_template_name": "学生日报", "allowAddReceivers": 1,
            "lastModifiedTime": 1,
            "report_template_fields": [{"field_name": "今日完成", "field_sort": 0, "field_type": 1}]}


class FakeDws:
    """Read-only-capable DWS stand-in; counts every real send."""

    def __init__(self, outcome="verified", template=None):
        self.outcome = outcome
        self.submits = 0
        self.template_payload = template or _template()
        self.outbox_rows = []

    def auth(self):
        return {"corp_id": "corp", "user_id": "self", "user_name": "Self"}

    def template(self, name, ident):
        return self.template_payload

    def contact(self, ident):
        return {"orgUserName": "Recipient", "orgName": "lab"}

    def outbox(self, name, start, end):
        return list(self.outbox_rows)

    def submit(self, ident, contents, users, dry_run=False):
        self.submits += 1
        if self.outcome == "timeout":
            raise DwsBlocked("DWS timeout; outcome unknown")
        if self.outcome == "no-id":
            return None
        return "report-1"

    def detail(self, ident):
        return {"report_template_id": "t", "reportId": ident,
                "report_content": [{"key": "今日完成", "value": "Done"}]}


def ready(tmp_path, monkeypatch, outcome="verified", day=DAY, kind="daily"):
    """Build a submit-ready draft inside ``<data_dir>/drafts`` with a fake DWS."""
    import lablog.cli as cli
    template = _template()
    cfg = {"_base": str(tmp_path), "data_dir": "data", "dws": {"profile": "p"},
           "identity": {"corp_id": "corp", "user_id": "self", "corp_name": "lab", "user_name": "Self"},
           "recipients": [{"name": "Recipient", "user_id": "r", "corp_name": "lab"}],
           "templates": {kind: {"id": "t", "name": "学生日报"}}, "history_from": "2026-10-01",
           "date_encoding": {"verified": True, "format": "iso-date"}}
    draft = freeze_draft({"kind": kind, "date": day, "template_id": "t", "template_name": "学生日报",
                          "template_fingerprint": template_fingerprint(template),
                          "fields": [{"name": "今日完成", "type": 1, "sort": 0, "value": "Done"}],
                          "blockers": [], "approval_context": cli._approval_context(cfg, kind, day)})
    path = tmp_path / "data" / "drafts" / f"{kind}-{day}.json"
    atomic_json(path, draft)
    fake = FakeDws(outcome)
    monkeypatch.setattr(cli, "_client", lambda config: fake)
    return cfg, path, draft, fake


def _mirror(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))["submission"]


def _auto_cfg(tmp_path):
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


def run_child(code, *args, timeout=90, tmp_path=None):
    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8")
    if tmp_path is not None:
        for key in ("TMPDIR", "TEMP", "TMP"):
            env[key] = str(tmp_path)
    return subprocess.run([sys.executable, "-X", "utf8", "-c", code, *args], env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, shell=False)


def wait_for(path, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if Path(path).exists():
            return True
        time.sleep(0.02)
    return False


# --------------------------------------------------------------------------
# P0-1..P0-3 locks
# --------------------------------------------------------------------------

def test_lock_busy_is_also_a_submission_block():
    assert issubclass(LockBusy, SubmissionBlocked)


def test_lock_is_released_and_owner_is_recorded(tmp_path):
    target = tmp_path / "draft.json"
    with file_lock(target) as owner:
        assert owner["pid"] == os.getpid()
        assert lock_path_for(target).is_file()
        assert read_lock_owner(lock_path_for(target))["token"] == owner["token"]
    assert not lock_path_for(target).exists()


def test_second_process_cannot_take_a_held_lock(tmp_path):
    """Two processes contend for the same draft: the loser waits, never corrupts."""
    target = tmp_path / "draft.json"
    gate = tmp_path / "gate"
    release = tmp_path / "release"
    code = """
import json, os, sys, time
from pathlib import Path
from lablog.workflow import file_lock
target, gate, release = sys.argv[1], sys.argv[2], sys.argv[3]
with file_lock(target, timeout=10):
    Path(gate).write_text("held", encoding="utf-8")
    deadline = time.time() + 20
    while not Path(release).exists() and time.time() < deadline:
        time.sleep(0.02)
print(json.dumps({"held": True}))
"""
    proc = subprocess.Popen([sys.executable, "-X", "utf8", "-c", code, str(target), str(gate), str(release)],
                            env=dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8"),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=False)
    try:
        assert wait_for(gate), "child never acquired the lock"
        with pytest.raises(LockBusy):
            with file_lock(target, timeout=0.4):
                pass
        assert lock_path_for(target).is_file()
        release.write_text("go", encoding="utf-8")
        assert proc.wait(timeout=20) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    with file_lock(target, timeout=2):
        atomic_json(target, {"hello": "world"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"hello": "world"}


def test_stale_lock_is_reclaimed_after_the_holder_is_killed(tmp_path):
    """A hard kill leaves the lock file behind; the next run must recover."""
    target = tmp_path / "draft.json"
    gate = tmp_path / "gate"
    code = """
import os, sys, time
from pathlib import Path
from lablog.workflow import file_lock
target, gate = sys.argv[1], sys.argv[2]
with file_lock(target, timeout=10):
    Path(gate).write_text(str(os.getpid()), encoding="utf-8")
    while True:
        time.sleep(0.05)
"""
    proc = subprocess.Popen([sys.executable, "-X", "utf8", "-c", code, str(target), str(gate)],
                            env=dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8"),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=False)
    try:
        assert wait_for(gate), "child never acquired the lock"
        killed_pid = int(gate.read_text(encoding="utf-8"))
        proc.kill()
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    assert lock_path_for(target).is_file(), "killed process should leave the lock behind"
    assert process_alive(killed_pid) is False
    before = read_lock_owner(lock_path_for(target))
    with file_lock(target, timeout=3) as owner:
        after = read_lock_owner(lock_path_for(target))
        assert after["token"] == owner["token"] != before["token"]
    assert not lock_path_for(target).exists()


def test_lock_owned_by_a_live_process_is_never_stolen(tmp_path):
    target = tmp_path / "draft.json"
    lock = lock_path_for(target)
    lock.write_text(json.dumps({"schema": 1, "pid": os.getpid(), "host": "same", "token": "foreign"}),
                    encoding="utf-8")
    with pytest.raises(LockBusy):
        with file_lock(target, timeout=0.3):
            pass
    assert lock.is_file(), "a lock held by a live process must never be deleted"
    assert read_lock_owner(lock)["token"] == "foreign"


def test_release_never_deletes_another_owners_lock(tmp_path):
    target = tmp_path / "draft.json"
    lock = lock_path_for(target)
    with file_lock(target) as owner:
        lock.write_text(json.dumps({"schema": 1, "pid": os.getpid(), "token": "someone-else"}),
                        encoding="utf-8")
        assert release_lock(lock, owner) is False
    assert lock.is_file()


def test_empty_or_legacy_lock_files_are_not_treated_as_free(tmp_path):
    target = tmp_path / "draft.json"
    lock = lock_path_for(target)
    lock.write_text("", encoding="utf-8")
    with pytest.raises(LockBusy):
        with file_lock(target, timeout=0.3):
            pass
    lock.write_text(str(os.getpid()), encoding="utf-8")  # legacy plain-pid lock, still alive
    with pytest.raises(LockBusy):
        with file_lock(target, timeout=0.3):
            pass
    assert lock.is_file()


def test_multi_lock_uses_one_global_order(tmp_path):
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    with multi_lock(second, first) as order:
        assert order == sorted(order)
        assert lock_path_for(first).is_file() and lock_path_for(second).is_file()
    assert not lock_path_for(first).exists() and not lock_path_for(second).exists()


# --------------------------------------------------------------------------
# P0-4 atomic writes
# --------------------------------------------------------------------------

def test_temp_names_are_unique_per_write(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    seen = []
    real = os.replace

    def spy(source, destination):
        seen.append(str(source))
        return real(source, destination)

    monkeypatch.setattr(os, "replace", spy)
    atomic_json(target, {"a": 1})
    atomic_json(target, {"a": 2})
    assert len(seen) == 2 and seen[0] != seen[1]
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2}


def test_failed_replace_keeps_the_last_valid_file_and_no_temp(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    atomic_json(target, {"good": 1})
    before = target.read_bytes()
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("disk gone")))
    with pytest.raises(OSError):
        atomic_json(target, {"bad": 2})
    assert target.read_bytes() == before
    assert list(target.parent.glob("*.tmp")) == []
    diagnostics = list(diagnostics_dir(target).glob("write-*.json"))
    assert diagnostics, "a failed write must leave a diagnostic record"
    assert "disk gone" in diagnostics[0].read_text(encoding="utf-8")


def test_unserializable_payload_leaves_the_file_untouched(tmp_path):
    target = tmp_path / "state.json"
    atomic_json(target, {"good": 1})
    before = target.read_bytes()
    cyclic = {}
    cyclic["self"] = cyclic
    with pytest.raises(ValueError):
        atomic_json(target, cyclic)
    assert target.read_bytes() == before
    assert list(target.parent.glob("*.tmp")) == []


def test_backup_restores_a_corrupted_primary(tmp_path):
    target = tmp_path / "evidence" / "daily-2026-10-14.json"
    atomic_json(target, {"facts": [0]}, backup=True)
    atomic_json(target, {"facts": [1]}, backup=True)  # second write keeps the previous valid copy
    assert target.with_name(target.name + ".bak").is_file()
    target.write_text("{not json", encoding="utf-8")
    document, meta = load_json_durable(target)
    # the backup is the last valid copy taken *before* the damaged write
    assert document == {"facts": [0]}
    assert meta["source"] == "backup" and meta["recovered"] is True
    assert json.loads(target.read_text(encoding="utf-8")) == {"facts": [0]}
    assert list(target.parent.glob("*.corrupt-*"))


def test_corrupt_document_without_backup_raises_value_error(tmp_path):
    target = tmp_path / "draft.json"
    target.write_text("garbage", encoding="utf-8")
    with pytest.raises(CorruptDocument):
        load_json_durable(target)


def test_atomic_text_writes_utf8_content(tmp_path):
    target = tmp_path / "preview.md"
    atomic_text(target, "# 日报\n内容\n")
    assert target.read_text(encoding="utf-8") == "# 日报\n内容\n"


def test_sweep_temp_files_skips_live_owners(tmp_path, monkeypatch):
    live = tmp_path / f"draft.json.{os.getpid()}.tmp"
    live.write_text("x", encoding="utf-8")
    dead = tmp_path / "draft.json.999997.tmp"
    dead.write_text("x", encoding="utf-8")
    old = time.time() - 7200
    for path in (live, dead):
        os.utime(path, (old, old))
    monkeypatch.setattr("lablog.workflow.process_alive", lambda pid, host=None: pid == os.getpid())
    found = {path.name for path in sweep_temp_files(tmp_path, max_age_seconds=3600)}
    assert live.name not in found
    assert dead.name in found


# --------------------------------------------------------------------------
# P0-5..P0-7 submission transaction
# --------------------------------------------------------------------------

def test_restart_after_interrupted_submit_is_reconciled_without_resending(tmp_path, monkeypatch):
    """A crash after the request was armed: recovery must be read-only."""
    import lablog.cli as cli
    cfg, path, draft, fake = ready(tmp_path, monkeypatch)
    state_path = cli._state_path(cfg, draft)
    state.write_attempt(state_path, status="submitting", phase=state.PHASE_REQUEST_PENDING,
                        draft_path=str(path.resolve()), draft_hash=draft["hash"], template_id="t",
                        reason="armed before the network call")
    fake.outbox_rows = [{"reportId": "report-9", "createTime": 1}]
    fake.detail = lambda ident: {"report_template_id": "t", "reportId": ident, "report_content": [
        {"key": "今日完成", "value": "Done"}, {"key": "工作日期", "value": DAY}]}
    result = cli._reconcile(cfg, path)
    assert result["status"] == "verified" and result["report_id"] == "report-9"
    assert fake.submits == 0, "recovery must never send a second report"
    assert _mirror(path)["status"] == "verified"
    assert state.read_state(state_path)["status"] == "verified"
    with pytest.raises(SubmissionBlocked, match="period"):
        cli._submit(cfg, path, draft["hash"])
    assert fake.submits == 0


def test_unresolved_restart_stays_manual_and_preserves_both_files(tmp_path, monkeypatch):
    import lablog.cli as cli
    cfg, path, draft, fake = ready(tmp_path, monkeypatch)
    state_path = cli._state_path(cfg, draft)
    state.write_attempt(state_path, status="submitting", phase=state.PHASE_REQUEST_PENDING,
                        draft_path=str(path.resolve()), draft_hash=draft["hash"], template_id="t")
    before_state = state_path.read_bytes()
    before_draft = path.read_bytes()
    result = cli._reconcile(cfg, path)
    assert result["status"] == "manual_review_required"
    assert state_path.read_bytes() == before_state and path.read_bytes() == before_draft
    assert state.read_state(state_path)["status"] == "submitting", "never downgrade an attempt"
    assert fake.submits == 0


def test_unknown_outcome_is_never_retried_and_mails_once(tmp_path, monkeypatch):
    import lablog.cli as cli
    cfg = _auto_cfg(tmp_path)
    sent = []
    monkeypatch.setattr(cli, "send_notification", lambda *a, **k: sent.append(a) or {"status": "sent"})
    draft = freeze_draft({"kind": "daily", "date": DAY, "template_id": "t", "template_name": "学生日报",
                          "template_fingerprint": template_fingerprint(_template()), "fields": [],
                          "blockers": [], "approval_context": cli._approval_context(cfg, "daily", DAY)})
    draft["submission"] = {"status": "unknown", "report_id": None}
    path = tmp_path / "data" / "drafts" / f"daily-{DAY}.json"
    atomic_json(path, draft)
    outcome = cli._auto(cfg, "daily", DAY, "extractive", submit=True)
    assert outcome["status"] == "unknown"
    assert len(sent) == 1, "an unknown outcome must reach a human exactly once"
    assert outcome["auto_submitted"] is False


def test_abort_before_the_request_leaves_no_attempt_and_allows_retry(tmp_path, monkeypatch):
    import lablog.cli as cli
    cfg, path, draft, fake = ready(tmp_path, monkeypatch)
    real_preflight = cli._preflight
    monkeypatch.setattr(cli, "_preflight", lambda *a, **k: (_ for _ in ()).throw(
        SubmissionBlocked("pre-flight refused")))
    with pytest.raises(SubmissionBlocked):
        cli._submit(cfg, path, draft["hash"])
    state_path = cli._state_path(cfg, draft)
    assert not state_path.exists(), "no state file means the request was never sent"
    assert "submission" not in json.loads(path.read_text(encoding="utf-8"))
    monkeypatch.setattr(cli, "_preflight", real_preflight)
    assert cli._submit(cfg, path, draft["hash"])["status"] == "verified"
    assert fake.submits == 1


@pytest.mark.parametrize("mirror_status,state_status,expect_forward_repair", [
    ("submitting", "verified", True),
    ("verified", "submitting", False),
])
def test_divergent_state_and_draft_are_blocked(tmp_path, monkeypatch, mirror_status, state_status,
                                               expect_forward_repair):
    import lablog.cli as cli
    cfg, path, draft, fake = ready(tmp_path, monkeypatch)
    state_path = cli._state_path(cfg, draft)
    state.write_attempt(state_path, status=state_status,
                        phase=state.PHASE_COMPLETED if state_status == "verified" else state.PHASE_REQUEST_PENDING,
                        draft_path=str(path.resolve()), draft_hash=draft["hash"], template_id="t",
                        report_id="report-1")
    current = json.loads(path.read_text(encoding="utf-8"))
    current["submission"] = {"status": mirror_status, "report_id": "report-1"}
    atomic_json(path, current)
    with pytest.raises(SubmissionBlocked, match="period|inconsistent"):
        cli._submit(cfg, path, current["hash"])
    assert fake.submits == 0
    if not expect_forward_repair:
        # The remote cannot confirm the stronger claim: nothing may be concluded.
        fake.detail = lambda ident: {"report_template_id": "t", "reportId": ident,
                                     "report_content": [{"key": "今日完成", "value": "Different"}]}
    result = cli._reconcile(cfg, path)
    if expect_forward_repair:
        assert result["status"] == "verified"
        assert _mirror(path)["status"] == "verified"
    else:
        assert result["status"] == "manual_review_required"
        assert _mirror(path)["status"] == "verified", "a stronger mirror is never downgraded"


def test_mismatched_attempt_ids_block_the_period(tmp_path, monkeypatch):
    import lablog.cli as cli
    cfg, path, draft, fake = ready(tmp_path, monkeypatch)
    state_path = cli._state_path(cfg, draft)
    state.write_attempt(state_path, status="submitting", phase=state.PHASE_REQUEST_PENDING,
                        attempt_id="attempt-a", draft_path=str(path.resolve()), draft_hash=draft["hash"])
    current = json.loads(path.read_text(encoding="utf-8"))
    current["submission"] = {"status": "submitting", "attempt_id": "attempt-b"}
    atomic_json(path, current)
    record = state.load_period(state_path=state_path, draft=current, draft_path=path)
    assert record.consistent is False and record.inconsistency == "attempt_id_mismatch"
    with pytest.raises(SubmissionBlocked, match="review before submit"):
        cli._submit(cfg, path, current["hash"])
    assert fake.submits == 0


def test_legacy_state_migrates_without_losing_fields(tmp_path, monkeypatch):
    import lablog.cli as cli
    cfg = _auto_cfg(tmp_path)
    sent = []
    monkeypatch.setattr(cli, "send_notification", lambda *a, **k: sent.append(a) or {"status": "sent"})
    monkeypatch.setattr(cli, "_client", lambda config: pytest.fail("must not touch DWS"))
    draft = freeze_draft({"kind": "daily", "date": DAY, "template_id": "t", "template_name": "学生日报",
                          "template_fingerprint": "f", "fields": [], "blockers": []})
    draft["submission"] = {"status": "verified", "report_id": "report-1"}
    path = tmp_path / "data" / "drafts" / f"daily-{DAY}.json"
    atomic_json(path, draft)
    state_path = cli._state_path(cfg, draft)
    atomic_json(state_path, {"status": "verified", "draft": str(path), "hash": draft["hash"],
                             "report_id": "report-1"})  # v1 shape
    record = state.read_state(state_path)
    assert record["schema_version"] == state.SCHEMA_VERSION
    assert record["phase"] == state.PHASE_COMPLETED
    assert record["report_id"] == "report-1" and record["draft_path"] == str(path)
    outcome = cli._auto(cfg, "daily", DAY, "extractive", submit=True)
    assert outcome["status"] == "already_submitted" and outcome["report_id"] == "report-1"
    assert sent == []


def test_cross_day_periods_are_isolated_but_a_week_is_shared(tmp_path, monkeypatch):
    import lablog.cli as cli
    cfg, first_path, first_draft, fake = ready(tmp_path, monkeypatch, day="2026-10-14")
    assert cli._submit(cfg, first_path, first_draft["hash"])["status"] == "verified"
    cfg2, second_path, second_draft, fake2 = ready(tmp_path, monkeypatch, day="2026-10-15")
    assert cli._submit(cfg2, second_path, second_draft["hash"])["status"] == "verified"
    assert fake.submits == 1 and fake2.submits == 1  # different days are independent periods
    cfg3, monday_path, monday_draft, fake3 = ready(tmp_path, monkeypatch, day="2026-10-12", kind="weekly")
    assert cli._submit(cfg3, monday_path, monday_draft["hash"])["status"] == "verified"
    cfg4, friday_path, friday_draft, _ = ready(tmp_path, monkeypatch, day="2026-10-16", kind="weekly")
    with pytest.raises(SubmissionBlocked, match="period"):
        cli._submit(cfg4, friday_path, friday_draft["hash"])
    assert fake3.submits == 1  # one period, one submission
