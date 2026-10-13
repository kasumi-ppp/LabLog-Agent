"""Shared work-period labels used by report generation and remote reconciliation."""

from __future__ import annotations

import re
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from .sources import period_bounds


_PERIOD_MARKER = re.compile(
    r"统计周期\s*[：:]\s*(\d{4}-\d{2}-\d{2})\s*(?:至|到|—|–|\.\.)\s*(\d{4}-\d{2}-\d{2})"
)
CHINA = ZoneInfo("Asia/Shanghai")


def period_labels(kind: str, day: str | date) -> tuple[str, str]:
    report_day = date.fromisoformat(day) if isinstance(day, str) else day
    start, end = period_bounds(kind, report_day)
    end_inclusive = end - timedelta(microseconds=1)
    return start.astimezone(CHINA).date().isoformat(), end_inclusive.astimezone(CHINA).date().isoformat()


def format_period_marker(kind: str, day: str | date) -> str:
    start, end = period_labels(kind, day)
    return f"统计周期：{start} 至 {end}"


def parse_period_marker(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    matches = set(_PERIOD_MARKER.findall(value))
    return next(iter(matches)) if len(matches) == 1 else None
