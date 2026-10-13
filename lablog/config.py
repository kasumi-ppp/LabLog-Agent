"""Local configuration and provenance checked calendar."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import yaml


def load_config(path):
    path = Path(path).resolve()
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("config must be a mapping")
    config["_base"] = str(path.parent.parent if path.parent.name == "config" else path.parent)
    return config


def resolve_path(config, value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else Path(config["_base"]) / path


def calendar_for(config, day):
    cal = config.get("calendar") or {}
    value = cal.get("file")
    if not value:
        raise ValueError("calendar file not configured")
    data = json.loads(resolve_path(config, value).read_text(encoding="utf-8"))
    if data.get("year") != day.year or not data.get("source"):
        raise ValueError("calendar year or provenance missing")
    return data


def is_workday(config, day):
    cal = calendar_for(config, day)
    iso = day.isoformat()
    if iso in cal.get("makeup_workdays", []):
        return True
    if iso in cal.get("holidays", []):
        return False
    return day.weekday() < 5


def schedule_due(config, kind, day):
    schedule = config.get("schedule") or {}
    if schedule.get("auto_submit"):
        raise ValueError("unattended auto-submit is unsupported without signed bounded approval")
    calendar_for(config, day)
    if kind == "daily":
        return is_workday(config, day)
    if kind == "weekly":
        return day.weekday() == int(schedule.get("weekly_weekday", 4))
    if kind == "monthly":
        configured = schedule.get("monthly_day", "last")
        last = (day + timedelta(days=1)).month != day.month
        return last if configured == "last" else day.day == int(configured)
    raise ValueError("unknown kind")
