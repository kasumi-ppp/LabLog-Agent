"""Read-only metadata and filtered event adapter for WorkBuddy sessions."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .sources import DONE, PROGRESS, SECRET, _canonical_message


_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_INCOMPLETE = {"incomplete", "in_progress", "in-progress", "running", "streaming", "cancelled", "canceled", "failed"}


def _iso_millis(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _safe_text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 6000 or SECRET.search(value):
        return None
    return value.strip()


def discover_workbuddy(root: Path) -> dict:
    """Read session metadata from ``root/workbuddy.db`` without modifying it."""
    root = Path(root).expanduser()
    db_path = root / "workbuddy.db"
    if not db_path.is_file():
        return {"conversations": [], "warnings": ["WorkBuddy database unavailable"], "reachable": False}

    try:
        encoded = quote(db_path.resolve().as_posix(), safe="/:\\")
        conn = sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, cwd, title, custom_title, created_at, updated_at, last_activity_at, "
                "deleted_at, is_playground FROM sessions"
            ).fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError):
        return {"conversations": [], "warnings": ["WorkBuddy database unreadable or invalid"], "reachable": False}

    conversations = []
    warnings = []
    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        return {"conversations": [], "warnings": ["WorkBuddy source unavailable"], "reachable": False}

    for row in rows:
        sid = row["id"]
        if not isinstance(sid, str) or not _SESSION_ID.fullmatch(sid):
            continue
        created = _iso_millis(row["created_at"])
        updated = _iso_millis(row["last_activity_at"]) or _iso_millis(row["updated_at"])
        if created is None or updated is None:
            warnings.append("WorkBuddy session has invalid timestamps")
            continue

        found_path = None
        escaped = False
        for candidate in (resolved_root / "projects").glob(f"*/{sid}.jsonl"):
            try:
                resolved = candidate.resolve(strict=True)
                if not resolved.is_relative_to(resolved_root):
                    escaped = True
                    continue
                if resolved.is_file():
                    found_path = str(resolved)
                    break
            except OSError:
                continue
        if escaped:
            warnings.append("WorkBuddy log path outside source root was ignored")

        title = row["custom_title"] or row["title"] or sid
        conversations.append({
            "id": sid,
            "source": "workbuddy",
            "cwd": row["cwd"] if isinstance(row["cwd"], str) else "",
            "title": str(title),
            "path": found_path,
            "created_at": created,
            "updated_at": updated,
            "archived": False,
            "deleted": row["deleted_at"] is not None,
            "temporary": bool(row["is_playground"]),
        })
    return {"conversations": conversations, "warnings": warnings, "reachable": True}


def parse_workbuddy_event(row: dict, session_id: str) -> dict | None:
    """Convert one event to a bounded, safe summary; omit raw tool inputs/outputs."""
    if not isinstance(row, dict) or row.get("sessionId") != session_id:
        return None
    event_id = row.get("id")
    stamp = _iso_millis(row.get("timestamp"))
    if not isinstance(event_id, (str, int)) or not str(event_id).strip() or stamp is None:
        return None

    event_type = row.get("type")
    if event_type == "message":
        role = row.get("role")
        if role not in {"user", "assistant"}:
            return None
        blocks = row.get("content")
        if not isinstance(blocks, list):
            return None
        # WorkBuddy's generic text blocks are adapted to the canonical source
        # shape. Reasoning, system, image, and other block types never enter it.
        allowed_types = {"input_text", "output_text", "text"}
        safe_blocks = []
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") not in allowed_types:
                continue
            text = block.get("text")
            if isinstance(text, str):
                block_type = block["type"]
                if block_type == "text":
                    block_type = "input_text" if role == "user" else "output_text"
                safe_blocks.append({"type": block_type, "text": text})
        channel = row.get("channel") or row.get("phase")
        canonical = _canonical_message({
            "type": "response_item",
            "payload": {"type": "message", "role": role, "channel": channel, "content": safe_blocks},
        })
        if canonical is None:
            return None
        _, _, text = canonical
        raw_status = row.get("status")
        message_status = raw_status if isinstance(raw_status, str) else ""
        if role == "user":
            status = "planned"
        elif message_status.casefold() in _INCOMPLETE:
            status = "in_progress"
        else:
            status = "completed" if DONE.search(text) else "in_progress" if PROGRESS.search(text) else "unknown"
        return {"event_id": str(event_id), "kind": "message", "role": role, "time": stamp,
                "text": text, "status": status, "message_status": message_status}

    if event_type in {"function_call", "function_call_result"}:
        call_id = row.get("callId")
        name = _safe_text(row.get("name"))
        if not isinstance(call_id, (str, int)) or not str(call_id).strip() or name is None:
            return None
        actual_status = row.get("status")
        actual_status = actual_status if isinstance(actual_status, str) else ""
        summary = name if event_type == "function_call" else f"{name}: {actual_status}".strip()
        if SECRET.search(summary):
            return None
        return {"event_id": str(event_id), "kind": "tool", "role": "tool", "time": stamp,
                "text": summary, "status": "planned" if event_type == "function_call" else "unknown",
                "message_status": actual_status, "tool_name": name, "call_id": str(call_id)}
    return None
