"""Read-only bridge adapter for explicitly selected ChatGPT snapshots."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .sources import DONE, PROGRESS, _canonical_message


_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_INCOMPLETE = {"incomplete", "in_progress", "in-progress", "running", "streaming", "cancelled", "canceled", "failed"}
_REFERENCE = "::chatgpt-content-reference"


def _iso(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value / 1000 if value > 100000000000 else value, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        return None
    return stamp.astimezone(timezone.utc).isoformat()


def _safe_id(value):
    return isinstance(value, str) and _SESSION_ID.fullmatch(value) is not None


def read_snapshot(path, session_id) -> dict:
    """Read one selected snapshot, rejecting path escapes and mismatched IDs."""
    path = Path(path)
    if not _safe_id(session_id) or path.name != f"{session_id}.json":
        raise OSError("invalid ChatGPT snapshot path")
    try:
        resolved = path.resolve(strict=True)
        parent = path.parent.resolve(strict=True)
        if resolved.parent != parent or resolved.name != path.name or not resolved.is_file():
            raise OSError("ChatGPT snapshot path outside expected location")
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OSError("ChatGPT snapshot unreadable or invalid") from exc
    if not isinstance(data, dict) or not isinstance(data.get("thread"), dict):
        raise OSError("ChatGPT snapshot structure invalid")
    if data["thread"].get("id") != session_id:
        raise OSError("ChatGPT snapshot ID mismatch")
    if not isinstance(data.get("turns"), list) or not isinstance(data.get("synced_at"), str):
        raise OSError("ChatGPT snapshot structure invalid")
    return data


def _has_reference(snapshot):
    for turn in snapshot.get("turns", []):
        if not isinstance(turn, dict):
            continue
        for item in turn.get("items", []) if isinstance(turn.get("items"), list) else []:
            if isinstance(item, dict) and item.get("type") == "agentMessage":
                if _REFERENCE in str(item.get("text", "")):
                    return True
    return False


def discover_chatgpt(root, session_ids, max_age_hours=24) -> dict:
    """Read metadata for explicit IDs only; never enumerate the snapshot root."""
    root = Path(root).expanduser()
    conversations, warnings = [], []
    reachable = True
    try:
        resolved_root = root.resolve(strict=True)
        if not resolved_root.is_dir():
            raise OSError
    except OSError:
        return {"conversations": [], "warnings": ["ChatGPT snapshot source unavailable"], "reachable": False}

    now = datetime.now(timezone.utc)
    for sid in session_ids:
        if not _safe_id(sid):
            warnings.append("invalid ChatGPT session ID ignored")
            reachable = False
            continue
        candidate = resolved_root / f"{sid}.json"
        try:
            snapshot = read_snapshot(candidate, sid)
        except OSError:
            warnings.append(f"ChatGPT snapshot missing or invalid: {sid}")
            reachable = False
            continue

        thread = snapshot["thread"]
        created = _iso(thread.get("createdAt"))
        updated = _iso(thread.get("updatedAt"))
        synced = _iso(snapshot.get("synced_at"))
        if created is None or updated is None or synced is None:
            warnings.append(f"ChatGPT snapshot timestamps invalid: {sid}")
            reachable = False
            continue
        try:
            age_hours = (now - datetime.fromisoformat(synced)).total_seconds() / 3600
            stale = age_hours > max_age_hours
        except (TypeError, ValueError):
            stale = True
        if stale:
            warnings.append(f"ChatGPT snapshot is stale: {sid}")
            reachable = False
        if snapshot.get("partial") or _has_reference(snapshot):
            warnings.append(f"ChatGPT snapshot contains content references; message text may be incomplete: {sid}")

        title = thread.get("title")
        if not isinstance(title, str) or not title.strip():
            title = sid
        conversations.append({
            "id": sid,
            "source": "chatgpt",
            "cwd": "ChatGPT",
            "title": title,
            "path": str(candidate.resolve()),
            "created_at": created,
            "updated_at": updated,
            "archived": False,
            "deleted": False,
            "temporary": False,
        })
    return {"conversations": conversations, "warnings": warnings, "reachable": reachable}


def snapshot_events(snapshot, session_id):
    """Flatten an already-read snapshot into timestamped safe message rows."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("thread"), dict) or snapshot["thread"].get("id") != session_id:
        return []
    events = []
    for turn in snapshot.get("turns", []):
        if not isinstance(turn, dict):
            continue
        for item in turn.get("items", []) if isinstance(turn.get("items"), list) else []:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "userMessage":
                role, stamp = "user", turn.get("startedAt")
                blocks = item.get("content")
                text = "\n".join(block.get("text", "") for block in blocks if isinstance(block, dict) and block.get("type") == "text") if isinstance(blocks, list) else ""
            elif item_type == "agentMessage":
                role, stamp = "assistant", turn.get("completedAt") or turn.get("startedAt")
                text = item.get("text")
                if isinstance(text, str) and _REFERENCE in text:
                    continue
            else:
                continue
            event_id = item.get("id")
            if not isinstance(event_id, (str, int)) or not str(event_id).strip() or not isinstance(text, str):
                continue
            event = {"id": str(event_id), "sessionId": session_id, "type": "message",
                     "role": role, "text": text, "time": stamp}
            if role == "assistant" and not turn.get("completedAt"):
                event["status"] = "in_progress"
            events.append(event)
    return events


def parse_chatgpt_event(row: dict, session_id: str) -> dict | None:
    """Convert one flattened bridge message to a bounded canonical event."""
    if not isinstance(row, dict) or row.get("sessionId") != session_id or row.get("type") != "message":
        return None
    event_id = row.get("id")
    stamp = _iso(row.get("time"))
    role = row.get("role")
    text = row.get("text")
    if not isinstance(event_id, (str, int)) or not str(event_id).strip() or stamp is None or role not in {"user", "assistant"} or not isinstance(text, str):
        return None
    if _REFERENCE in text:
        return None
    canonical = _canonical_message({
        "type": "response_item",
        "payload": {"type": "message", "role": role,
                    "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]},
    })
    if canonical is None:
        return None
    _, _, safe_text = canonical
    raw_status = row.get("status")
    message_status = raw_status if isinstance(raw_status, str) else ""
    if role == "user":
        status = "planned"
    elif message_status.casefold() in _INCOMPLETE:
        status = "in_progress"
    else:
        status = "completed" if DONE.search(safe_text) else "in_progress" if PROGRESS.search(safe_text) else "unknown"
    return {"event_id": str(event_id), "kind": "message", "role": role, "time": stamp,
            "text": safe_text, "status": status, "message_status": message_status}
