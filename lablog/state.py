"""Authoritative per-period submission state, heartbeats and pending alerts.

The state file below ``<data_dir>/state/`` is the single source of truth for
"has a submission been attempted for this work period". The ``submission`` block
inside a draft is only a mirror kept for humans and for older drafts.

Three invariants every caller must respect:

I1  While a state record exists with a status in ``ATTEMPTED``, no code path may
    send a real submission. Only read-only checks (detail / outbox / dry-run)
    are allowed.
I2  No automatic path may downgrade a recorded attempt or clear it. Clearing is
    a deliberate human action.
I3  A missing state file means the request was never sent (pre-flight refused
    before the network call). An existing one means the request *may* have been
    sent, so the period is never retried: the only way to reach ``verified`` is
    a remote read-back that matches.
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import resolve_path
from .workflow import CorruptDocument, atomic_json, load_json_durable

SCHEMA_VERSION = 2
PHASE_IDLE = "idle"
PHASE_REQUEST_PENDING = "request_pending"  # armed: the request may already be in flight
PHASE_ID_CONFIRMED = "id_confirmed"        # a report id is known
PHASE_READBACK_MISMATCH = "readback_mismatch"
PHASE_COMPLETED = "completed"

TERMINAL = {"verified", "submitted"}
ATTEMPTED = {"submitting", "unknown", "mismatch", "verified", "submitted"}
UNRESOLVED = {"unknown", "mismatch", "submitting"}

_LEGACY_PHASE = {"submitting": PHASE_REQUEST_PENDING, "unknown": PHASE_REQUEST_PENDING,
                 "mismatch": PHASE_READBACK_MISMATCH, "verified": PHASE_COMPLETED,
                 "submitted": PHASE_COMPLETED}


def data_root(cfg):
    return Path(resolve_path(cfg, cfg.get("data_dir", "data")))


def state_dir(cfg):
    return data_root(cfg) / "state"


def heartbeat_dir(cfg):
    return state_dir(cfg) / "heartbeat"


def alerts_dir(cfg):
    return data_root(cfg) / "diagnostics" / "alerts"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# state records
# --------------------------------------------------------------------------

def migrate(raw):
    """Upgrade a v1 record (``status``/``draft``/``hash``/``report_id``)."""
    if not isinstance(raw, dict):
        raise CorruptDocument("state record is not a JSON object")
    status = raw.get("status") or "unknown"
    history = raw.get("history") or []
    return {**raw, "schema_version": SCHEMA_VERSION,
            "status": status,
            "phase": raw.get("phase") or _LEGACY_PHASE.get(status, PHASE_REQUEST_PENDING),
            "attempt_id": raw.get("attempt_id"),
            "state_version": int(raw.get("state_version") or len(history) or 1),
            "history": history,
            "draft_path": raw.get("draft_path") or raw.get("draft"),
            "draft_hash": raw.get("draft_hash") or raw.get("hash"),
            "contents_hash": raw.get("contents_hash"),
            "report_id": raw.get("report_id"),
            "template_id": raw.get("template_id"),
            "owner": raw.get("owner") or {},
            "error": raw.get("error")}


def read_state(path):
    """Read and migrate a state record; ``None`` when the file does not exist."""
    path = Path(path)
    if not path.exists():
        return None
    document, _ = load_json_durable(path)
    return migrate(document)


def write_attempt(path, *, status, phase, attempt_id=None, draft_path=None, draft_hash=None,
                  template_id=None, contents_hash=None, report_id=None, reason=None, **extra):
    """Create the record that marks the request as possibly in flight."""
    path = Path(path)
    attempt_id = attempt_id or uuid.uuid4().hex
    record = {"schema_version": SCHEMA_VERSION, "attempt_id": attempt_id, "state_version": 1,
              "status": status, "phase": phase, "created_at": _now(), "updated_at": _now(),
              "last_phase_at": _now(), "draft_path": str(draft_path) if draft_path else None,
              "draft_hash": draft_hash, "template_id": template_id,
              "contents_hash": contents_hash, "report_id": report_id,
              "owner": {"pid": os.getpid(), "host": socket.gethostname()},
              "error": None, "reason": reason,
              "history": [{"at": _now(), "state_version": 1, "from": None, "to": status,
                           "phase": phase, "reason": reason}], **extra}
    atomic_json(path, record, integrity=True, backup=True)
    return record


def transition(path, *, to_status=None, phase=None, report_id=None, reason=None, error=None, **extra):
    """Move an existing record forward; history and version are appended."""
    path = Path(path)
    current = read_state(path)
    if current is None:
        raise CorruptDocument(f"cannot transition a missing state file: {path}")
    before = current.get("status")
    version = int(current.get("state_version") or len(current.get("history") or []) or 1) + 1
    current["state_version"] = version
    current["updated_at"] = _now()
    current["schema_version"] = SCHEMA_VERSION
    if to_status:
        current["status"] = to_status
    if phase:
        current["phase"] = phase
        current["last_phase_at"] = _now()
    if report_id is not None:
        current["report_id"] = report_id
    if reason is not None:
        current["reason"] = reason
    if error is not None:
        current["error"] = error
    current.update(extra)
    history = list(current.get("history") or [])
    history.append({"at": _now(), "state_version": version, "from": before,
                    "to": current["status"], "phase": current.get("phase"), "reason": reason})
    current["history"] = history
    atomic_json(path, current, integrity=True, backup=True)
    return current


def period_key(cfg, draft):
    """Stable identity of a work period (mirrors ``cli._period_identity``)."""
    import hashlib
    from .sources import period_bounds
    from datetime import date
    start, end = period_bounds(draft["kind"], date.fromisoformat(draft["date"]))
    identity = "\0".join((str((draft.get("approval_context") or {}).get("profile")),
                          str(draft.get("template_id")), str(draft.get("kind")),
                          start.isoformat(), end.isoformat()))
    return hashlib.sha256(identity.encode()).hexdigest()


# --------------------------------------------------------------------------
# consistency
# --------------------------------------------------------------------------

@dataclass
class PeriodRecord:
    status: str | None            # authoritative status (None = no attempt yet)
    attempt_id: str | None
    phase: str | None
    report_id: str | None
    authority: str                # 'state' | 'draft' | 'none'
    consistent: bool = True
    inconsistency: str | None = None
    legacy: bool = False
    draft_status: str | None = None
    can_submit: bool = False
    state_path: str | None = None
    raw: dict | None = None


def _mirror(draft):
    submission = (draft or {}).get("submission") or {}
    return submission.get("status"), submission.get("attempt_id")


def load_period(*, state_path=None, draft=None, draft_path=None):
    """Decide what a period is currently doing from the authoritative files."""
    state_path = Path(state_path) if state_path else None
    draft_status, draft_attempt = _mirror(draft)
    record = None
    unreadable = False
    if state_path and state_path.exists():
        try:
            record = read_state(state_path)
        except (CorruptDocument, ValueError, OSError):
            unreadable = True
    if unreadable:
        return PeriodRecord(status="unknown", attempt_id=None, phase=PHASE_REQUEST_PENDING,
                            report_id=None, authority="state", consistent=False,
                            inconsistency="state_unreadable", draft_status=draft_status,
                            can_submit=False, state_path=str(state_path))
    if record is None:
        if draft_status in ATTEMPTED:
            # Legacy: an attempt recorded only inside the draft (pre state-file).
            return PeriodRecord(status=draft_status, attempt_id=draft_attempt,
                                phase=_LEGACY_PHASE.get(draft_status, PHASE_REQUEST_PENDING),
                                report_id=(draft or {}).get("submission", {}).get("report_id"),
                                authority="draft", legacy=True, draft_status=draft_status,
                                can_submit=False, state_path=str(state_path) if state_path else None)
        return PeriodRecord(status=None, attempt_id=None, phase=PHASE_IDLE, report_id=None,
                            authority="none", draft_status=draft_status, can_submit=True,
                            state_path=str(state_path) if state_path else None)
    status = record.get("status")
    attempt_id = record.get("attempt_id")
    result = PeriodRecord(status=status, attempt_id=attempt_id, phase=record.get("phase"),
                          report_id=record.get("report_id"), authority="state",
                          draft_status=draft_status, raw=record,
                          state_path=str(state_path) if state_path else None)
    if draft is None:
        # The draft is gone; a terminal period is still done, anything else is
        # suspicious and must be looked at rather than recreated silently.
        if status in TERMINAL:
            result.inconsistency = "draft_missing"
            result.consistent = False
    elif draft_attempt and attempt_id and draft_attempt != attempt_id:
        result.consistent = False
        result.inconsistency = "attempt_id_mismatch"
    elif draft_status in TERMINAL and status not in TERMINAL:
        # The mirror is stronger than the authority: never downgrade a verdict,
        # and never trust a mirror that the authority cannot confirm.
        result.consistent = False
        result.inconsistency = "mirror_stronger_than_state"
    elif status in TERMINAL and draft_status not in TERMINAL:
        result.inconsistency = "mirror_lagging"  # forward repair is allowed
    result.can_submit = bool(result.consistent and status not in ATTEMPTED)
    return result


def block_reason(record):
    if record.inconsistency == "state_unreadable":
        return ("the recorded submission state for this report period is unreadable; "
                "reconcile manually before submit")
    if record.inconsistency == "attempt_id_mismatch":
        return ("local state and draft disagree about which attempt this report period belongs to; "
                "review before submit")
    if record.inconsistency == "mirror_stronger_than_state":
        return ("the draft claims a stronger outcome than the recorded submission state for this "
                "report period; reconcile remotely before submit")
    if record.inconsistency:
        return f"this report period has an inconsistent local submission state ({record.inconsistency})"
    return f"report period already has a local submission attempt ({record.status})"


def iter_states(cfg):
    """Yield ``(path, record_or_None, error)`` for every state file."""
    directory = state_dir(cfg)
    if not directory.is_dir():
        return []
    rows = []
    for path in sorted(directory.glob("*.json")):
        try:
            rows.append((path, read_state(path), None))
        except (CorruptDocument, ValueError, OSError) as exc:
            rows.append((path, None, f"{type(exc).__name__}: {exc}"))
    return rows


# --------------------------------------------------------------------------
# heartbeats and alerts
# --------------------------------------------------------------------------

def heartbeat_start(cfg, task, *, kind=None, run_id=None):
    path = heartbeat_dir(cfg) / f"{task}.json"
    payload = {"task": task, "kind": kind, "run_id": run_id or uuid.uuid4().hex,
               "pid": os.getpid(), "host": socket.gethostname(),
               "started_at": _now(), "finished_at": None, "status": "running", "outcome": None}
    atomic_json(path, payload, integrity=True)
    return payload


def heartbeat_finish(cfg, task, outcome, *, run_id=None):
    path = heartbeat_dir(cfg) / f"{task}.json"
    payload = {"task": task, "run_id": run_id, "pid": os.getpid(), "host": socket.gethostname(),
               "finished_at": _now(), "status": "finished", "outcome": outcome}
    if path.exists():
        try:
            previous, _ = load_json_durable(path)
            payload = {**previous, **payload}
        except (CorruptDocument, ValueError, OSError):
            pass
    atomic_json(path, payload, integrity=True)
    return payload


def read_heartbeats(cfg):
    directory = heartbeat_dir(cfg)
    if not directory.is_dir():
        return []
    rows = []
    for path in sorted(directory.glob("*.json")):
        try:
            document, _ = load_json_durable(path)
            rows.append(document)
        except (CorruptDocument, ValueError, OSError):
            rows.append({"task": path.stem, "unreadable": True})
    return rows


def record_alert(cfg, name, body):
    """Persist an alert before trying to mail it; mail delivery may be broken."""
    directory = alerts_dir(cfg)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = directory / f"{name}-{stamp}.md"
    path.write_text(str(body), encoding="utf-8")
    return path


def pending_alerts(cfg):
    directory = alerts_dir(cfg)
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.md"))


def consume_alerts(cfg, paths):
    removed = []
    for path in paths:
        try:
            Path(path).unlink()
            removed.append(str(path))
        except OSError:
            continue
    return removed


def as_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
