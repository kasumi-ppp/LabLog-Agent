"""Immutable draft approval, durable writes and cross-process file locks.

Durability rules enforced here:

* A lock records its owner (pid, host, token, time). A lock is only taken over
  when the recorded process is *proved* gone; lock age is diagnostic only.
* Files are replaced atomically and the previous *valid* copy is kept as
  ``.bak`` so an interrupted or corrupt write can still be recovered.
* Every failed write leaves a diagnostic record under
  ``<data_dir>/diagnostics``; diagnostics never raise themselves.
"""

from __future__ import annotations

import copy
import ctypes
import hashlib
import json
import os
import random
import re
import secrets
import socket
import sys
import tempfile
import time
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path


class SubmissionBlocked(Exception):
    pass


class LockBusy(SubmissionBlocked):
    """Another live process holds the lock, or its owner cannot be proved dead.

    Subclasses ``SubmissionBlocked`` on purpose: callers (``main``, ``_auto``)
    catch the base class, so an exhaustive lock never changes the exit code.
    """

    def __init__(self, message, *, lock=None, owner=None):
        super().__init__(message)
        self.lock = str(lock) if lock else None
        self.owner = owner or {}


class CorruptDocument(ValueError):
    """No valid copy of a persisted document could be recovered."""


ATOMIC_SCHEMA = 1
LOCK_SCHEMA = 1
DATA_FOLDERS = {"drafts", "state", "evidence", "previews", "backlog", "scheduler", "diagnostics"}
LEGACY_TEMP_PID = re.compile(r"\.(\d+)\.tmp$")


def draft_hash(draft):
    body = {k: v for k, v in draft.items() if k not in {"hash", "submission", "previewed_at", "preview_count"}}
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


# --------------------------------------------------------------------------
# diagnostics
# --------------------------------------------------------------------------

def diagnostics_dir(target):
    """Return ``<data_dir>/diagnostics`` for a file stored inside the data dir."""
    parent = Path(target).parent
    if parent.name in DATA_FOLDERS and str(parent.parent):
        return parent.parent / "diagnostics"
    return parent / "diagnostics"


def record_diagnostic(target, category, payload):
    """Append-one-file diagnostic record. Never raises, never overwrites."""
    try:
        directory = diagnostics_dir(target)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = directory / f"{category}-{stamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}.json"
        body = {"category": category, "target": str(target), "pid": os.getpid(),
                "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "payload": payload}
        path.write_text(json.dumps(body, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return path
    except Exception:  # diagnostics must never change the outcome of a run
        return None


# --------------------------------------------------------------------------
# process liveness
# --------------------------------------------------------------------------

def _pid_alive_posix(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


_STILL_ACTIVE = 259


def _pid_alive_windows(pid):
    """Windows: a handle alone proves nothing, the exit code decides.

    A terminated process stays resolvable while any handle is still open, so
    ``OpenProcess`` succeeding must not be read as "alive"; only
    ``STILL_ACTIVE`` means the process is still running.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: no such pid
            return False
        return True  # access denied and friends: cannot prove death
    try:
        code = ctypes.c_uint32()
        if not kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)):
            return True
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def process_alive(pid, host=None):
    """True alive, False proved dead, None undeterminable (other host)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return False
    if host and host != socket.gethostname():
        return None
    try:
        return _pid_alive_windows(pid) if os.name == "nt" else _pid_alive_posix(pid)
    except Exception:
        return None


# --------------------------------------------------------------------------
# file locks
# --------------------------------------------------------------------------

def lock_path_for(target):
    return Path(str(target) + ".lock")


def _owner_record():
    return {"schema": LOCK_SCHEMA, "pid": os.getpid(), "host": socket.gethostname(),
            "token": secrets.token_hex(8),
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "cmd": " ".join(sys.argv)[:200]}


def read_lock_owner(lock_path):
    """Read the owner record; tolerates legacy plain-pid and truncated files."""
    try:
        raw = Path(lock_path).read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        return None
    if not raw.strip():
        return {"pid": None, "token": None, "reason": "empty"}
    try:
        owner = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        text = raw.decode("ascii", "replace").strip()
        return {"pid": int(text) if text.isdigit() else None, "token": None, "reason": "legacy"}
    return owner if isinstance(owner, dict) else {"pid": None, "token": None, "reason": "not-object"}


def _owned_by_live_process(owner):
    """True when the lock must be respected (owner alive or undeterminable)."""
    if not owner:
        return True  # unknown owner: safest is to wait
    status = process_alive(owner.get("pid"), owner.get("host"))
    return True if status is None else bool(status)


def _take_over(lock, owner):
    """Atomically replace a lock believed stale; False when another won."""
    staging = lock.with_name(f"{lock.name}.takeover.{owner['pid']}.{owner['token']}")
    try:
        with staging.open("w", encoding="utf-8") as file:
            json.dump(owner, file)
            file.flush()
            os.fsync(file.fileno())
        try:
            os.replace(staging, lock)
        except OSError:
            _cleanup_temp(staging)
            return False
    except OSError:
        _cleanup_temp(staging)
        return False
    current = read_lock_owner(lock) or {}
    return current.get("token") == owner["token"]  # loser must not proceed


def acquire_lock(lock_path, timeout=5.0, poll=0.05, steal_stale=True):
    """Acquire the lock; returns the owner record used to release it."""
    lock = Path(lock_path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    owner = _owner_record()
    deadline = time.monotonic() + float(timeout)
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            peer = read_lock_owner(lock) or {}
            if steal_stale and not _owned_by_live_process(peer) and _take_over(lock, owner):
                return owner
            if time.monotonic() >= deadline:
                raise LockBusy(f"lock busy: {lock}", lock=lock, owner=peer)
            time.sleep(poll + random.uniform(0, poll))
            continue
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(owner, file)
                file.flush()
                os.fsync(file.fileno())
        except OSError as exc:
            record_diagnostic(lock, "lock", {"event": "owner_write_failed", "error": f"{type(exc).__name__}: {exc}"})
            raise
        return owner


def release_lock(lock_path, owner):
    """Delete the lock only when we still own it; never delete a foreign lock."""
    lock = Path(lock_path)
    current = read_lock_owner(lock) or {}
    if not current or current.get("token") != (owner or {}).get("token"):
        return False
    try:
        lock.unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        record_diagnostic(lock, "lock", {"event": "release_failed", "error": f"{type(exc).__name__}: {exc}"})
        return False


@contextmanager
def file_lock(path, timeout=5, poll=0.05, steal_stale=True):
    lock = lock_path_for(path)
    owner = acquire_lock(lock, timeout=float(timeout), poll=poll, steal_stale=steal_stale)
    try:
        yield owner
    finally:
        release_lock(lock, owner)


@contextmanager
def multi_lock(*paths, timeout=5, poll=0.05):
    """Acquire several locks in a fixed global order so no cycle can form."""
    unique = {str(Path(p)) for p in paths}
    ordered = sorted(unique, key=lambda value: str(lock_path_for(value)))
    with ExitStack() as stack:
        for path in ordered:
            stack.enter_context(file_lock(Path(path), timeout=float(timeout), poll=poll))
        yield ordered


# --------------------------------------------------------------------------
# atomic writes
# --------------------------------------------------------------------------

def _fsync_dir(directory):
    """Best effort directory fsync; Windows cannot open directories."""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return False
    try:
        os.fsync(fd)
        return True
    except OSError:
        return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _cleanup_temp(path):
    try:
        Path(path).unlink()
    except (FileNotFoundError, OSError):
        pass


def _replace_with_retry(temp, target, attempts=6, delay=0.02):
    last = None
    for _ in range(attempts):
        try:
            os.replace(temp, target)
            _fsync_dir(Path(target).parent)
            return
        except OSError as exc:  # Windows: indexer/AV may hold the target briefly
            last = exc
            time.sleep(delay)
            delay *= 2
    _cleanup_temp(temp)
    raise last


def _backup_valid_target(target, validate=None):
    """Copy the current file to ``.bak`` only when it is a valid document."""
    if not target.exists():
        return None
    try:
        payload = target.read_bytes()
        document = json.loads(payload.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if validate is not None:
        try:
            if not validate(document):
                return None
        except Exception:
            return None
    backup = target.with_name(target.name + ".bak")
    try:
        _write_bytes_atomic(backup, payload, backup=False)
        return backup
    except OSError:
        return None


def _write_bytes_atomic(target, payload, *, backup=False, validate_backup=None):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    made = _backup_valid_target(target, validate_backup) if backup else None
    fd, temp = tempfile.mkstemp(prefix=target.name + ".", suffix="." + uuid.uuid4().hex[:8] + ".tmp",
                                dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        _replace_with_retry(Path(temp), target)
    except BaseException as exc:
        _cleanup_temp(temp)
        record_diagnostic(target, "write", {"event": "write_failed", "error": f"{type(exc).__name__}: {exc}"})
        raise
    return made


def with_integrity(data):
    """Attach a schema version and a checksum that excludes the checksum itself."""
    body = {k: v for k, v in data.items() if k not in {"checksum", "schema_version"}}
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return {**data, "schema_version": ATOMIC_SCHEMA,
            "checksum": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}


def check_integrity(data):
    """Verify the checksum written by ``with_integrity``; missing means legacy."""
    if not isinstance(data, dict) or "checksum" not in data:
        return True
    expected = with_integrity(data).get("checksum")
    return expected == data.get("checksum")


def atomic_json(path, data, *, backup=False, validate_backup=None, integrity=False):
    payload = with_integrity(data) if (integrity and isinstance(data, dict)) else data
    try:
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError) as exc:
        record_diagnostic(path, "write", {"event": "serialize_failed", "error": f"{type(exc).__name__}: {exc}"})
        raise
    _write_bytes_atomic(Path(path), text.encode("utf-8"), backup=backup, validate_backup=validate_backup)
    return Path(path)


def atomic_text(path, content, *, backup=False, encoding="utf-8"):
    _write_bytes_atomic(Path(path), str(content).encode(encoding), backup=backup)
    return Path(path)


# --------------------------------------------------------------------------
# durable reads
# --------------------------------------------------------------------------

def _self_heal_from_backup(path, backup):
    """Rename the corrupt primary aside (never delete) and restore the backup."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    snapshot = path.with_name(path.name + f".corrupt-{stamp}")
    try:
        os.replace(path, snapshot)
    except OSError:
        snapshot = None
    try:
        _write_bytes_atomic(path, Path(backup).read_bytes(), backup=False)
    except OSError:
        pass
    return snapshot


def load_json_durable(path, *, validate=None, allow_backup=True):
    """Return ``(document, meta)``, falling back to the ``.bak`` copy.

    Raises ``CorruptDocument`` (a ``ValueError``) when no valid copy exists; the
    corrupt primary is kept as ``.corrupt-<timestamp>`` for a human to inspect.
    """
    path = Path(path)
    meta = {"path": str(path), "source": None, "recovered": False,
            "primary_error": None, "corrupt_snapshot": None}
    document, error = None, None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CorruptDocument(f"missing document: {path}") from exc
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    ok = document is not None and _passes(document, validate)
    if not ok and error is None:
        error = "document failed validation"
    if ok:
        meta["source"] = "primary"
        return document, meta
    meta["primary_error"] = error
    if allow_backup:
        backup = path.with_name(path.name + ".bak")
        if backup.exists():
            try:
                candidate = json.loads(backup.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                candidate = None
            if candidate is not None and _passes(candidate, validate):
                meta["corrupt_snapshot"] = str(_self_heal_from_backup(path, backup) or "")
                meta.update({"source": "backup", "recovered": True})
                record_diagnostic(path, "recover", {"event": "restored_from_backup", **meta})
                return candidate, meta
    raise CorruptDocument(f"no valid version of {path}: {meta['primary_error']}")


def _passes(document, validate):
    if validate is None:
        return isinstance(document, dict)
    try:
        return bool(validate(document))
    except Exception:
        return False


def sweep_temp_files(directory, *, max_age_seconds=86400):
    """List leftover temp files that are safe to inspect (never deletes)."""
    root = Path(directory)
    if not root.is_dir():
        return []
    now = time.time()
    stale = []
    for path in root.rglob("*.tmp"):
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age < max_age_seconds:
            continue
        match = LEGACY_TEMP_PID.search(path.name)
        if match and process_alive(int(match.group(1))):
            continue  # a live writer still owns it
        stale.append(path)
    return stale
