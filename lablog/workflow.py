"""Immutable draft approval and guarded submission state."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path


class SubmissionBlocked(Exception):
    pass


def draft_hash(draft):
    body = {k: v for k, v in draft.items() if k not in {"hash", "submission", "previewed_at"}}
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def freeze_draft(draft):
    result = copy.deepcopy(draft)
    result["hash"] = draft_hash(result)
    return result


def validate_submission(draft, confirm_hash, live):
    if not confirm_hash or confirm_hash != draft.get("hash"):
        raise SubmissionBlocked("confirm hash is required and must match the draft")
    if draft_hash(draft) != draft.get("hash"):
        raise SubmissionBlocked("draft hash changed")
    if draft.get("template_fingerprint") != live.get("template_fingerprint"):
        raise SubmissionBlocked("template fingerprint changed")
    if draft.get("submission", {}).get("status") in {"submitting", "unknown", "verified", "submitted", "mismatch"}:
        raise SubmissionBlocked("draft was already submitted or has unknown outcome")
    if draft.get("blockers"):
        raise SubmissionBlocked("draft has unresolved blockers: " + "; ".join(draft["blockers"]))
    return True


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temp.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2, default=str)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp, path)


def atomic_text(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temp.open("w", encoding="utf-8", newline="") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp, path)


@contextmanager
def file_lock(path, timeout=5):
    lock = Path(str(path) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise SubmissionBlocked(f"draft lock busy: {lock}")
            time.sleep(0.1)
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass
