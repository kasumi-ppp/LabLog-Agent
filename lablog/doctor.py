"""Read-only local diagnostics.

Answers the questions a maintainer asks after something went wrong: which data
directory is in use, did the last collection and generation succeed, what is the
current submission state, is a stale lock blocking anything, and are there
attempts waiting for a human. Never mutates a draft, a state file or evidence.
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import state
from .config import is_workday, resolve_path
from .workflow import (diagnostics_dir, lock_path_for, process_alive, read_lock_owner,
                       sweep_temp_files)

CHINA = ZoneInfo("Asia/Shanghai")


def _data_dir(cfg):
    return Path(resolve_path(cfg, cfg.get("data_dir", "data")))


def _writable(directory):
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _age_seconds(path):
    try:
        return round(time.time() - path.stat().st_mtime, 1)
    except OSError:
        return None


def _lock_report(root):
    rows = []
    for lock in sorted(root.rglob("*.lock")):
        owner = read_lock_owner(lock) or {}
        alive = process_alive(owner.get("pid"), owner.get("host"))
        rows.append({"lock": str(lock.relative_to(root)), "owner": owner,
                     "owner_alive": alive, "age_seconds": _age_seconds(lock)})
    return rows


def _evidence_report(root):
    rows = []
    directory = root / "evidence"
    if not directory.is_dir():
        return rows
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rows.append({"file": path.name, "unreadable": True})
            continue
        collected = data.get("collected_at")
        stale = None
        if collected:
            try:
                stamp = datetime.fromisoformat(collected)
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                stale = round((datetime.now(timezone.utc) - stamp).total_seconds() / 86400, 2)
            except ValueError:
                stale = None
        unreachable = [s["name"] for s in (data.get("sources") or [])
                       if s.get("configured") and not s.get("reachable")]
        rows.append({"file": path.name, "collected_at": collected, "stale_days": stale,
                     "facts": int(data.get("deduped_fact_count") if data.get("deduped_fact_count") is not None
                                  else len(data.get("facts") or [])),
                     "unreachable_sources": unreachable,
                     "retention": (data.get("retention") or {}).get("action"),
                     "rejected_at": data.get("last_rejected_at")})
    return rows


def _draft_state_pairs(cfg, root):
    pairs = []
    drafts = sorted((root / "drafts").glob("*.json")) if (root / "drafts").is_dir() else []
    for path in drafts:
        try:
            draft = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pairs.append({"draft": path.name, "unreadable": True, "issues": ["draft unreadable"]})
            continue
        try:
            state_path = root / "state" / (state.period_key(cfg, draft) + ".json")
        except (KeyError, ValueError, TypeError):
            state_path = None
        record = None
        if state_path and state_path.exists():
            try:
                record = state.read_state(state_path)
            except (ValueError, OSError):
                record = None
        issues = []
        mirror = (draft.get("submission") or {}).get("status")
        if record is None and mirror in state.ATTEMPTED:
            issues.append("legacy attempt recorded only in the draft")
        if record is not None:
            if record.get("status") != mirror and not (record.get("status") in state.TERMINAL
                                                       and mirror not in state.TERMINAL):
                issues.append(f"state says {record.get('status')} but the draft says {mirror}")
            if record.get("status") in state.UNRESOLVED:
                issues.append("submission outcome unresolved; needs a person")
        pairs.append({"draft": path.name, "status": record.get("status") if record else mirror,
                      "phase": (record or {}).get("phase"),
                      "report_id": (record or {}).get("report_id") or (draft.get("submission") or {}).get("report_id"),
                      "previewed_at": draft.get("previewed_at"),
                      "state_file": str(state_path.relative_to(root)) if state_path and state_path.exists() else None,
                      "issues": issues})
    return pairs


def _scheduler_runs(root, keep=10):
    path = root / "scheduler" / "runs.jsonl"
    if not path.is_file():
        return []
    rows = []
    try:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return []
    for line in lines[-keep:]:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def missed_runs(cfg, days=7):
    """Workdays in the look-back window with no finished auto heartbeat."""
    beats = state.read_heartbeats(cfg)
    finished = set()
    for beat in beats:
        for key in ("finished_at", "started_at"):
            value = beat.get(key)
            if not value:
                continue
            try:
                stamp = datetime.fromisoformat(value)
            except ValueError:
                continue
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            finished.add(stamp.astimezone(CHINA).date().isoformat())
    today = datetime.now(CHINA).date()
    missing, checked = [], []
    for offset in range(int(days)):
        day = today - timedelta(days=offset)
        try:
            if not is_workday(cfg, day):
                continue
        except (ValueError, OSError):
            continue  # calendar unavailable: do not invent an alarm
        checked.append(day.isoformat())
        if day.isoformat() not in finished:
            missing.append(day.isoformat())
    return {"checked_workdays": checked, "missing": missing,
            "heartbeats": [{"task": b.get("task"), "finished_at": b.get("finished_at"),
                            "outcome": b.get("outcome")} for b in beats]}


def resend_alerts(cfg, sender):
    """Try to deliver alerts that could not be mailed before."""
    from .notify import send as send_notification

    delivered, failed = [], []
    for path in state.pending_alerts(cfg):
        body = path.read_text(encoding="utf-8")
        result = send_notification(cfg, f"[LabLog] 补发告警 {path.stem}", body)
        if (result or {}).get("status") == "sent":
            delivered.append(str(path))
            state.consume_alerts(cfg, [path])
        else:
            failed.append({"alert": str(path), "result": result})
    return {"delivered": delivered, "failed": failed}


def collect(cfg):
    """Everything a maintainer needs, without touching a single document."""
    root = _data_dir(cfg)
    pairs = _draft_state_pairs(cfg, root)
    locks = _lock_report(root)
    temps = [str(p.relative_to(root)) for p in sweep_temp_files(root, max_age_seconds=3600)]
    alerts = [str(p.relative_to(root)) for p in state.pending_alerts(cfg)]
    issues = []
    if not _writable(root):
        issues.append(f"data directory is not writable: {root}")
    if any(row["owner_alive"] in (None, True) for row in locks):
        issues.append("a lock is held by a live or unidentifiable process")
    elif locks:
        issues.append("stale lock(s) found; the next run can reclaim them")
    if temps:
        issues.append("leftover temp files found")
    for pair in pairs:
        for issue in pair.get("issues") or []:
            issues.append(f"{pair['draft']}: {issue}")
    if alerts:
        issues.append("undelivered alert(s) stored locally")
    return {
        "data_dir": str(root),
        "data_dir_writable": _writable(root),
        "counts": {name: len(list((root / name).glob("*.json"))) if (root / name).is_dir() else 0
                   for name in ("drafts", "state", "evidence", "previews")},
        "locks": locks,
        "temp_files": temps,
        "drafts": pairs,
        "evidence": _evidence_report(root),
        "heartbeats": state.read_heartbeats(cfg),
        "scheduler_runs": _scheduler_runs(root),
        "pending_alerts": alerts,
        "needs_manual_review": [p for p in pairs if p.get("status") in state.UNRESOLVED
                                or any("person" in i or "inconsistent" in i or "but the draft" in i
                                       for i in (p.get("issues") or []))],
        "issues": issues,
    }
