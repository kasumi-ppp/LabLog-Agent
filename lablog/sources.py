"""Bounded, local work evidence. Raw conversations never leave this module."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import yaml

CHINA = ZoneInfo("Asia/Shanghai")
SECRET = re.compile(
    r'''(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)["']?\s*[:=]\s*'''
    r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\S+)|\bBearer\s+\S+|-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY-----'''
)
DONE = re.compile(r"(?i)(?:^|[。！\n])\s*(?:[-*]\s*)?(?:我)?(?:已完成|完成了|已实现|已修复|已提交|测试通过|实现完成|修复完成|implemented|fixed|completed|done|tests pass(?:ed)?)")
PROGRESS = re.compile(r"(?i)(?:正在|进行中|working on|in progress)")


def period_bounds(kind: str, day: date):
    if kind == "daily":
        first, last = day, day + timedelta(days=1)
    elif kind == "weekly":
        first = day - timedelta(days=day.weekday())
        last = first + timedelta(days=7)
    elif kind == "monthly":
        first = day.replace(day=1)
        last = first.replace(year=first.year + 1, month=1) if first.month == 12 else first.replace(month=first.month + 1)
    else:
        raise ValueError("kind must be daily, weekly, or monthly")
    return (datetime.combine(first, time.min, CHINA).astimezone(timezone.utc),
            datetime.combine(last, time.min, CHINA).astimezone(timezone.utc))


def _normal_path(value):
    path = str(value or "").replace("\\", "/").rstrip("/").casefold()
    if path.startswith("//?/unc/"):
        return "//" + path[8:]
    return path[4:] if path.startswith("//?/") else path


def _allowed(cwd, projects, excluded):
    cwd = _normal_path(cwd)
    if not cwd or any(cwd == x or cwd.startswith(x + "/") for x in map(_normal_path, excluded)):
        return False
    return bool(projects) and any(cwd == x or cwd.startswith(x + "/") for x in map(_normal_path, projects))


def _canonical_message(item):
    if item.get("type") != "response_item":
        return None
    p = item.get("payload") or {}
    if p.get("type") != "message" or p.get("role") not in {"user", "assistant"}:
        return None
    channel = p.get("channel") or p.get("phase")
    if p.get("role") == "assistant" and channel not in {None, "final", "final_answer", "commentary"}:
        return None
    if p.get("role") == "assistant" and channel is None and not all(
        isinstance(x, dict) and x.get("type") == "output_text" for x in p.get("content") or []
    ):
        return None
    text = "\n".join(x.get("text", "") for x in p.get("content") or [] if isinstance(x, dict) and x.get("type") in {"input_text", "output_text"}).strip()
    lowered = text.casefold()
    if text.startswith("{"):
        try:
            structured = json.loads(text)
        except json.JSONDecodeError:
            structured = None
        if isinstance(structured, dict) and {"risk_level", "user_authorization", "outcome", "rationale"} <= structured.keys():
            return None
    if any(marker in lowered for marker in ("<environment_context>", "<permissions instructions>", "<system-reminder>",
                                           "# agents.md", "the following is the codex agent history added since your last approval assessment",
                                           "auto approval review", ">>>transcript", "<external_codex_apps_open_page>",
                                           "gaaaa", "<retained", "<trusted", ">>>retained", ">>>trusted",
                                           "retained transcript", "trusted transcript", "begin codex transcript")):
        return None
    if re.search(r"(?m)^\[\d+\]\s+(?:tool|assistant|user|developer)\b", text, re.I):
        return None
    if not text or SECRET.search(text) or len(text) > 6000:
        return None
    return p.get("role"), channel, text


def collect_codex(roots, kind, day, *, project_paths=(), session_ids=(), exclude_paths=(), max_files=5000):
    """Collect canonical user/final-assistant messages in the requested window."""
    start, end = period_bounds(kind, day)
    facts, warnings, seen = [], [], set()
    files = []
    for root in roots:
        path = Path(root).expanduser()
        if path.is_file() and path.suffix == ".jsonl":
            files.append(path)
        elif path.is_dir():
            files.extend(path.rglob("*.jsonl"))
    files = sorted(set(files))
    if len(files) > max_files:
        warnings.append(f"session file limit reached; omitted {len(files) - max_files}")
    for path in files[:max_files]:
        try:
            with path.open("r", encoding="utf-8") as stream:
                rows = stream.readlines()
        except OSError as exc:
            warnings.append(f"unreadable session {path.name}: {type(exc).__name__}")
            continue
        meta = None
        for line in rows:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                warnings.append(f"partial JSON line in {path.name}")
                continue
            if obj.get("type") == "session_meta":
                meta = obj.get("payload") or {}
        if not meta:
            continue
        sid, cwd = str(meta.get("id") or path.stem), meta.get("cwd")
        if session_ids and sid not in session_ids:
            continue
        excluded = _normal_path(cwd) and any(_normal_path(cwd) == x or _normal_path(cwd).startswith(x + "/")
                                              for x in map(_normal_path, exclude_paths))
        explicitly_selected = sid in session_ids
        if excluded or (not explicitly_selected and not _allowed(cwd, project_paths, exclude_paths)):
            continue
        for index, line in enumerate(rows, 1):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            canonical = _canonical_message(obj)
            if canonical is None:
                continue
            try:
                stamp = datetime.fromisoformat(obj["timestamp"].replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    continue
                stamp = stamp.astimezone(timezone.utc)
            except (KeyError, TypeError, ValueError):
                continue
            if not start <= stamp < end:
                continue
            role, channel, value = canonical
            digest = hashlib.sha256((sid + "\0" + role + "\0" + value).encode()).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            status = ("in_progress" if channel == "commentary" else "completed" if channel in {"final", "final_answer"} and DONE.search(value) else "in_progress" if PROGRESS.search(value) else "unknown") if role == "assistant" else "planned"
            facts.append({"evidence_id": digest[:20], "source": "codex", "session_id": sid,
                          "project": cwd, "time": stamp.isoformat(), "role": role, "channel": channel,
                          "status": status, "text": value,
                          "source_line": index, "task_id": digest[:20], "title": value.splitlines()[0][:120],
                          "description": value, "date": stamp.astimezone(CHINA).date().isoformat(),
                          "source_type": "codex", "source_path": str(path), "commit_hash": None,
                          "evidence": [digest[:20]], "confidence": "medium" if status == "completed" else "low",
                          "notes": "assistant reported completion; not independently verified" if status == "completed" else "assistant statement; completion unverified" if role == "assistant" else "user request or plan"})
    return {"facts": sorted(facts, key=lambda x: x["time"]), "warnings": warnings}


def collect_git(paths, kind, day):
    start, end = period_bounds(kind, day)
    facts, warnings = [], []
    for root in paths:
        root = Path(root).expanduser()
        if not root.is_dir():
            warnings.append(f"missing git path: {root}")
            continue
        command = ["git", "-C", str(root), "log", "--format=%H%x09%cI%x09%s", "--since=" + start.isoformat(), "--before=" + end.isoformat(), "-n", "200"]
        try:
            proc = subprocess.run(command, capture_output=True, text=True, timeout=15, shell=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            warnings.append(f"git unavailable {root.name}: {type(exc).__name__}")
            continue
        if proc.returncode:
            warnings.append(f"git log failed: {root.name}")
            continue
        rows = proc.stdout.splitlines()
        if len(rows) >= 200:
            warnings.append(f"git commit limit reached: {root.name}")
        for row in rows:
            parts = row.split("\t", 2)
            if len(parts) != 3 or SECRET.search(parts[2]):
                continue
            sha, stamp, subject = parts
            facts.append({"evidence_id": sha, "source": "git", "project": str(root), "time": stamp,
                          "status": "completed", "text": subject, "task_id": sha[:20],
                          "title": subject[:120], "description": subject, "date": stamp[:10],
                          "source_type": "git", "source_path": str(root), "commit_hash": sha,
                          "evidence": [sha], "confidence": "high", "notes": "committed change, not deployment proof"})
        try:
            state = subprocess.run(["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=no"],
                                   capture_output=True, text=True, timeout=10, shell=False)
        except (OSError, subprocess.TimeoutExpired):
            state = None
        if state is not None and state.returncode == 0 and state.stdout.strip() and start <= datetime.now(timezone.utc) < end:
            files = [line[3:] for line in state.stdout.splitlines() if len(line) >= 4 and not SECRET.search(line)]
            if files:
                description = "Uncommitted changes in " + ", ".join(files[:20])
                stamp = datetime.now(timezone.utc)
                digest = hashlib.sha256((str(root) + description).encode()).hexdigest()[:20]
                facts.append({"evidence_id": digest, "source": "git", "project": str(root), "time": stamp.isoformat(),
                              "status": "in_progress", "text": description, "task_id": digest,
                              "title": "Uncommitted changes", "description": description,
                              "date": stamp.astimezone(CHINA).date().isoformat(), "source_type": "git",
                              "source_path": str(root), "commit_hash": None, "evidence": [digest],
                              "confidence": "medium", "notes": "working tree state, not completed work"})
    return {"facts": facts, "warnings": warnings}


def collect_markdown(paths, kind=None, day=None, *, max_files=500):
    facts, warnings = [], []
    bounds = period_bounds(kind, day) if kind and day else None
    for value in paths:
        explicit = Path(value).expanduser()
        if explicit.is_dir():
            files = sorted(set(list(explicit.rglob("*.md")) + list(explicit.rglob("*.markdown"))))
            if len(files) > max_files:
                warnings.append(f"markdown file limit reached: {explicit.name}; omitted {len(files) - max_files}")
            files = files[:max_files]
        elif explicit.is_file() and explicit.suffix.lower() in {".md", ".markdown"}:
            files = [explicit]
        else:
            warnings.append(f"missing markdown path: {explicit}")
            continue
        root = explicit.resolve() if explicit.is_dir() else explicit.resolve().parent
        for path in files:
            try:
                resolved = path.resolve(strict=True)
                if not resolved.is_relative_to(root):
                    warnings.append(f"markdown symlink escapes configured path: {path.name}")
                    continue
                with path.open("r", encoding="utf-8") as stream:
                    raw = stream.read(12001)
                if len(raw) > 12000:
                    warnings.append(f"markdown content limit reached: {path.name}; truncated after 12000 characters")
                    raw = raw[:12000]
            except (OSError, UnicodeError):
                warnings.append(f"markdown unreadable: {path.name}")
                continue
            metadata = {}
            content = raw
            if raw.startswith("---\n") or raw.startswith("---\r\n"):
                parts = raw.split("---", 2)
                if len(parts) == 3:
                    try:
                        metadata = yaml.safe_load(parts[1]) or {}
                    except yaml.YAMLError:
                        warnings.append(f"markdown frontmatter invalid: {path.name}")
                        continue
                    content = parts[2]
            declared = metadata.get("date") if isinstance(metadata, dict) else None
            if declared is None:
                match = re.search(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)", path.stem)
                declared = match.group(1) if match else None
            if declared is None:
                warnings.append(f"markdown explicit date missing: {path.name}")
                continue
            try:
                declared = date.fromisoformat(str(declared))
            except ValueError:
                warnings.append(f"markdown explicit date invalid: {path.name}")
                continue
            if bounds:
                local_stamp = datetime.combine(declared, time.min, CHINA)
                if not bounds[0] <= local_stamp.astimezone(timezone.utc) < bounds[1]:
                    continue
            content = content.strip()
            if not content or SECRET.search(content):
                warnings.append(f"markdown skipped due to empty or sensitive content: {path.name}")
                continue
            status = metadata.get("status", "unknown") if isinstance(metadata, dict) else "unknown"
            if status not in {"completed", "in_progress", "planned", "unknown"}:
                warnings.append(f"markdown status invalid: {path.name}")
                continue
            digest = hashlib.sha256(content.encode()).hexdigest()[:20]
            facts.append({"evidence_id": digest, "source": "markdown", "path": str(path),
                          "time": datetime.combine(declared, time.min, CHINA).isoformat(), "status": status, "text": content,
                          "task_id": digest, "title": content.splitlines()[0][:120], "description": content,
                          "date": declared.isoformat(), "source_type": "markdown",
                          "source_path": str(path), "commit_hash": None, "evidence": [digest],
                          "confidence": "medium" if status == "completed" else "low", "notes": "explicit dated note; status self-reported"})
    return {"facts": facts, "warnings": warnings}
