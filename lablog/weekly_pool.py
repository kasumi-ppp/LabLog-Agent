"""Bounded pool of completed work available to supplement weekly reports."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .backlog import char_count
from .config import resolve_path
from .workflow import atomic_json, file_lock

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _path(cfg):
    return Path(resolve_path(cfg, cfg.get("data_dir", "data"))) / "weekly-pool" / "items.json"


def _empty():
    return {"schema_version": 1, "items": [], "receipts": []}


def _read(path):
    if not path.exists():
        return _empty()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid weekly pool file: {path}") from exc
    if (not isinstance(value, dict) or value.get("schema_version") != 1 or
            not isinstance(value.get("items"), list) or not isinstance(value.get("receipts"), list)):
        raise ValueError(f"invalid weekly pool structure: {path}")
    for item in value["items"]:
        if (not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k]
                for k in ("id", "text", "work_date", "week_start")) or
                not isinstance(item.get("evidence_ids"), list) or not isinstance(item.get("provenance"), list)):
            raise ValueError(f"invalid weekly pool item: {path}")
    for receipt in value["receipts"]:
        if (not isinstance(receipt, dict) or not isinstance(receipt.get("item_ids"), list) or
                not all(isinstance(x, str) for x in receipt["item_ids"])):
            raise ValueError(f"invalid weekly pool receipt: {path}")
    return value


def load(cfg):
    """Read a validated snapshot; corrupt data is surfaced and never erased."""
    path = _path(cfg)
    with file_lock(path):
        return _read(path)


def _week(day):
    d = day if isinstance(day, date) else date.fromisoformat(day)
    return d - timedelta(days=d.weekday())


def _work_date(fact):
    value = fact.get("date")
    if value:
        return date.fromisoformat(str(value)[:10])
    value = fact.get("time")
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=_SHANGHAI)
        return stamp.astimezone(_SHANGHAI).date()
    except ValueError:
        return None


def _normalized(text):
    return " ".join(str(text).split()).casefold()


def _fact_item(fact):
    status = str(fact.get("status", "")).casefold()
    role = str(fact.get("role", "")).casefold()
    text = str(fact.get("text", "")).strip()
    if status not in {"completed", "committed"} or role == "user" or not text:
        return None
    work_date = _work_date(fact)
    if work_date is None:
        return None
    normalized = _normalized(text)
    if not normalized:
        return None
    ident = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    evidence = fact.get("evidence_ids")
    if isinstance(evidence, str):
        evidence = [evidence]
    elif isinstance(evidence, list):
        evidence = list(evidence)
    else:
        evidence = []
    if not evidence and fact.get("evidence_id"):
        evidence = [fact["evidence_id"]]
    provenance = {k: fact[k] for k in ("source", "session_id", "message_id", "project", "source_path", "source_line")
                  if fact.get(k) is not None}
    supporting = fact.get("supporting_evidence") or []
    if isinstance(supporting, list):
        for source in supporting:
            if not isinstance(source, dict):
                continue
            provenance_ids = [source.get("evidence_id")] if source.get("evidence_id") else []
            evidence.extend(provenance_ids)
    provenances = ([provenance] if provenance else []) + [x for x in supporting if isinstance(x, dict)]
    return {"id": ident, "text": text, "work_date": work_date.isoformat(),
            "week_start": _week(work_date).isoformat(), "evidence_ids": list(dict.fromkeys(map(str, evidence))),
            "provenance": list({json.dumps(p, sort_keys=True, default=str): p for p in provenances}.values())}


def sync(cfg, day: str, facts: list) -> dict:
    """Replace the active candidate view with the supplied fresh authorized facts."""
    today_week = _week(day)
    earliest = today_week - timedelta(days=7)
    path = _path(cfg)
    with file_lock(path):
        data = _read(path)
        old = {item["id"]: item for item in data["items"]}
        consumed = {ident for receipt in data["receipts"] for ident in receipt["item_ids"]}
        grouped = {}
        for fact in facts:
            item = _fact_item(fact)
            if not item:
                continue
            work_day = date.fromisoformat(item["work_date"])
            if work_day < earliest or work_day >= today_week + timedelta(days=7) or item["id"] in consumed:
                continue
            previous = old.get(item["id"])
            if previous:
                # Keep the earliest origin date so rediscovery cannot extend expiry.
                if date.fromisoformat(previous["work_date"]) < work_day:
                    item["work_date"] = previous["work_date"]
                    item["week_start"] = previous["week_start"]
            # Apply the retention window after preserving the original date.
            origin_week = date.fromisoformat(item["week_start"])
            if (origin_week < earliest or origin_week > today_week or
                    date.fromisoformat(day) >= origin_week + timedelta(days=14)):
                continue
            existing = grouped.get(item["id"])
            if existing:
                if item["work_date"] < existing["work_date"]:
                    existing["work_date"], existing["week_start"] = item["work_date"], item["week_start"]
                existing["evidence_ids"] = list(dict.fromkeys(existing["evidence_ids"] + item["evidence_ids"]))
                existing["provenance"] = list({json.dumps(p, sort_keys=True): p for p in existing["provenance"] + item["provenance"]}.values())
            else:
                grouped[item["id"]] = item
        data["items"] = sorted(grouped.values(), key=lambda x: (x["work_date"], x["id"]))
        atomic_json(path, data)
        return {"active": len(data["items"]), "removed": len(old.keys() - grouped.keys()),
                "expired": sum(date.fromisoformat(i["week_start"]) + timedelta(days=14) <= date.fromisoformat(day)
                               for i in old.values())}


def select(cfg, day: str, needed_chars: int, exclude_evidence_ids=(), exclude_texts=()) -> list:
    if needed_chars <= 0:
        return []
    data = load(cfg)
    current = _week(day)
    excluded_ids = set(map(str, exclude_evidence_ids))
    excluded_texts = {_normalized(t) for t in exclude_texts}
    consumed = {ident for receipt in data["receipts"] for ident in receipt["item_ids"]}
    rows = sorted(data["items"], key=lambda x: (0 if x["week_start"] == current.isoformat() else 1,
                                                x["work_date"], x["id"]))
    selected, chars = [], 0
    for item in rows:
        origin_week = date.fromisoformat(item["week_start"])
        if (origin_week < current - timedelta(days=7) or origin_week > current or
                date.fromisoformat(day) >= origin_week + timedelta(days=14)):
            continue
        if item["id"] in consumed or excluded_ids.intersection(item["evidence_ids"]):
            continue
        if _normalized(item["text"]) in excluded_texts:
            continue
        selected.append(item)
        chars += char_count(item["text"])
        if chars >= needed_chars:
            break
    return selected


def mark_reported(cfg, day: str, item_ids: list, report_id: str) -> int:
    """Consume selected items only after the caller verifies remote submission."""
    path = _path(cfg)
    with file_lock(path):
        data = _read(path)
        key = (_week(day).isoformat(), str(report_id))
        receipt = next((r for r in data["receipts"]
                        if (r.get("period_start"), r.get("report_id")) == key), None)
        prior = set(receipt["item_ids"]) if receipt else set()
        already = {ident for row in data["receipts"] for ident in row["item_ids"]} - prior
        accepted = list(dict.fromkeys(str(x) for x in item_ids if str(x) not in already))
        added = [ident for ident in accepted if ident not in prior]
        if receipt:
            receipt["item_ids"] = list(dict.fromkeys(receipt["item_ids"] + accepted))
        else:
            data["receipts"].append({"period_start": key[0], "report_id": key[1], "item_ids": accepted})
        atomic_json(path, data)
        return len(added)
