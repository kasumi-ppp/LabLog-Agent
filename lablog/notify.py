"""Best-effort operator notification for unattended runs.

A scheduled run has no human watching it, so every stop reason is also pushed to
the operator's mailboxes through the same authenticated DWS profile that submits
the report. Notification is a side channel only: it never raises, and it never
changes the report outcome.
"""

from __future__ import annotations

import json
import subprocess


def _succeeded(stdout):
    try:
        data = json.loads(stdout)
    except (TypeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    if data.get("success") is True or data.get("success") == "true":
        return True
    return data.get("errcode") == 0


def send(cfg, subject, body):
    """Send one plain-text mail. Returns a status dict and never raises."""
    notify = cfg.get("notify") or {}
    if not notify.get("enabled"):
        return {"status": "disabled"}
    dws = cfg.get("dws") or {}
    executable, profile = dws.get("executable"), dws.get("profile")
    sender = notify.get("from")
    recipients = [str(x) for x in (notify.get("to") or []) if str(x).strip()]
    if not executable or not profile or not sender or not recipients:
        return {"status": "misconfigured", "reason": "notify.from/dws.executable/dws.profile required"}
    argv = [str(executable), "--profile", str(profile), "--format", "json",
            "mail", "message", "send",
            "--from", str(sender), "--to", ",".join(recipients),
            "--subject", str(subject), "--content", str(body)]
    try:
        proc = subprocess.run(argv, text=True, encoding="utf-8", errors="replace", capture_output=True,
                              timeout=int(notify.get("timeout_seconds", 60)), shell=False)
    except subprocess.TimeoutExpired:
        return {"status": "failed", "reason": "timeout"}
    except OSError as exc:
        return {"status": "failed", "reason": type(exc).__name__}
    if proc.returncode == 0 and _succeeded(proc.stdout):
        return {"status": "sent", "recipients": recipients}
    return {"status": "failed", "reason": (proc.stdout or proc.stderr or "")[:300]}
