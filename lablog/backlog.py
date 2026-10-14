"""Local backlog for sustainable daily-report prose.

Holds report body text that exceeded the configured target length so it can be
submitted later on days that have no fresh work evidence. Entries are keyed by a
content hash, kept in a single JSON file under ``<data_dir>/backlog/items.json``
and moved from ``pending`` to ``consumed`` only once a submission is verified.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path

from .config import resolve_path
from .workflow import atomic_json, file_lock

VERSION = 1
CITATION = re.compile(r"\s*\[证据:([^\]]+)\]")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VALID_STATUSES = ("pending", "consumed")
# Kind separates finished work from plans; plans must never be substituted for a
# day's completed work. Legacy entries without a kind are treated as completed.
KIND_COMPLETED = "completed"
KIND_PLAN = "plan"
VALID_KINDS = (KIND_COMPLETED, KIND_PLAN)


def normalize_kind(value) -> str:
    """Coerce a stored or incoming kind to a known value (default completed)."""
    return value if value in VALID_KINDS else KIND_COMPLETED


def char_count(text) -> int:
    """Count meaningful characters: citations and all whitespace are ignored.

    Non-string input counts as empty text so callers can pass through optional
    fields without a type guard. This is the project-wide length measure.
    """
    if not isinstance(text, str):
        return 0
    stripped = CITATION.sub("", text)
    return sum(1 for char in stripped if not char.isspace())


def _path(cfg) -> Path:
    """Return the absolute path of the backlog items file."""
    root = resolve_path(cfg, cfg.get("data_dir", "data"))
    return root / "backlog" / "items.json"


def _empty() -> dict:
    return {"version": VERSION, "items": []}


def load(cfg) -> dict:
    """Read the backlog file; a missing file is an empty backlog, not an error."""
    path = _path(cfg)
    if not path.exists():
        return _empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"backlog file is unreadable: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"backlog file must be a JSON object: {path}")
    if "items" not in data:
        raise ValueError(f"backlog file is missing the items key: {path}")
    if not isinstance(data["items"], list):
        raise ValueError(f"backlog items must be a list: {path}")
    return data


def _valid_date(value) -> bool:
    """True when value is a real calendar date in strict YYYY-MM-DD form."""
    if not isinstance(value, str) or not DATE_PATTERN.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def append(cfg, items) -> int:
    """Queue new text items, skipping blanks, bad dates and known ids.

    Returns the number of items actually added. Duplicates (pending or already
    consumed) are neither re-queued nor overwritten.
    """
    if not isinstance(items, list):
        raise ValueError("append expects a list of items")
    incoming = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        created_date = item.get("created_date")
        if not _valid_date(created_date):
            continue
        source = item.get("source") or "unknown"
        evidence_ids = item.get("evidence_ids") or []
        if not isinstance(evidence_ids, list):
            evidence_ids = []
        incoming.append({
            "id": hashlib.sha256(text.encode("utf-8")).hexdigest()[:20],
            "text": text,
            "created_date": created_date,
            "source": str(source),
            "evidence_ids": [str(x) for x in evidence_ids],
            "kind": normalize_kind(item.get("kind")),
            "status": "pending",
            "consumed_date": None,
            "consumed_report_id": None,
        })
    if not incoming:
        return 0
    path = _path(cfg)
    with file_lock(path):
        data = load(cfg)
        known = {entry.get("id") for entry in data["items"]}
        added = 0
        for entry in incoming:
            if entry["id"] in known:
                continue
            data["items"].append(entry)
            known.add(entry["id"])
            added += 1
        if added:
            data["version"] = VERSION
            atomic_json(path, data)
        return added


def _pending_sorted(data, day=None, kind=None):
    """Pending entries, oldest first, optionally limited to days up to ``day``.

    ``kind`` filters by entry kind when given (``None`` means no filter); legacy
    entries without a kind count as completed.
    """
    pending = [entry for entry in data["items"] if entry.get("status") == "pending"]
    if kind is not None:
        pending = [entry for entry in pending if normalize_kind(entry.get("kind")) == kind]
    if day is not None:
        pending = [entry for entry in pending if str(entry.get("created_date", "")) <= day]
    pending.sort(key=lambda entry: (str(entry.get("created_date", "")), str(entry.get("id", ""))))
    return pending


def take(cfg, limit_chars, date=None, kind=KIND_COMPLETED) -> tuple:
    """Select pending entries that fit within ``limit_chars`` total characters.

    ``kind`` defaults to ``"completed"`` so a caller backfilling a day's work
    never receives plan text; pass ``None`` to take every kind. Never mutates the
    file: consumption is recorded separately by ``mark_consumed`` so a failed
    submission keeps its backlog. Returns
    ``(selected, selected_chars, remaining_pending)``.
    """
    data = load(cfg)
    pending = _pending_sorted(data, day=date, kind=kind)
    selected = []
    total = 0
    for entry in pending:
        size = char_count(entry.get("text"))
        if limit_chars > 0 and selected and total + size > limit_chars:
            break
        if limit_chars > 0 and not selected and size > limit_chars:
            selected.append(entry)
            total += size
            break
        selected.append(entry)
        total += size
    return selected, total, len(pending) - len(selected)


def mark_consumed(cfg, ids, date, report_id) -> int:
    """Flag the given pending ids as consumed; returns how many changed."""
    if not ids:
        return 0
    wanted = {str(x) for x in ids}
    path = _path(cfg)
    with file_lock(path):
        data = load(cfg)
        changed = 0
        for entry in data["items"]:
            if entry.get("id") in wanted and entry.get("status") == "pending":
                entry["status"] = "consumed"
                entry["consumed_date"] = date
                entry["consumed_report_id"] = report_id
                changed += 1
        if changed:
            atomic_json(path, data)
        return changed


def stats(cfg) -> dict:
    """Summarize the backlog: pending volume, per-kind chars, consumed and oldest."""
    data = load(cfg)
    pending = [entry for entry in data["items"] if entry.get("status") == "pending"]
    consumed = [entry for entry in data["items"] if entry.get("status") == "consumed"]
    by_kind = {KIND_COMPLETED: 0, KIND_PLAN: 0}
    for entry in pending:
        by_kind[normalize_kind(entry.get("kind"))] += char_count(entry.get("text"))
    days = [str(entry.get("created_date")) for entry in pending if entry.get("created_date")]
    return {
        "pending_items": len(pending),
        "pending_chars": sum(char_count(entry.get("text")) for entry in pending),
        "pending_chars_by_kind": by_kind,
        "consumed_items": len(consumed),
        "oldest": min(days) if days else None,
    }
