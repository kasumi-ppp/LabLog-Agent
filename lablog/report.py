"""Conservative mapping of evidence into the three existing templates."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from datetime import date, timedelta
from zoneinfo import ZoneInfo
import re

from .dws import template_fields, template_fingerprint
from .sources import period_bounds
from .workflow import freeze_draft

FIELDS = {
    "daily": {"completed": "今日完成", "summary": "心得体会", "plan": "明日计划", "coordination": "需协调工作", "date": "工作日期", "hours": "总工时/h"},
    "weekly": {"completed": "本周完成工作", "summary": "本周工作总结", "plan": "下周工作计划", "coordination": "需协调与帮助"},
    "monthly": {"completed": "本月工作内容", "summary": "本月工作总结", "plan": "下月工作计划", "coordination": "需帮助与支持"},
}
PLAN_INTENT = re.compile(r"(?i)(?:明天|下周|下月|计划|打算|准备|待办|todo|to-do|next\s+(?:day|week|month))")


def _evidence_lines(facts, statuses):
    lines = []
    for fact in facts:
        if fact.get("status") not in statuses:
            continue
        text = str(fact.get("text", "")).strip().replace("\n", " ")
        if not text:
            continue
        if len(text) > 500:
            text = text[:500] + "…"
        qualifier = "（会话报告，未独立核验）" if fact.get("source") == "codex" and fact.get("status") == "completed" else ""
        lines.append(f"- {text}{qualifier} [证据:{fact['evidence_id']}]")
    return "\n".join(lines)


def build_draft(kind, day, template, evidence, *, manual_text=None, hours=None, ai_result=None):
    if kind not in FIELDS:
        raise ValueError("unknown report kind")
    names = FIELDS[kind]
    facts = evidence.get("facts", [])
    committed = _evidence_lines(facts, {"completed", "committed"})
    blockers = []
    if not committed:
        blockers.append("no dated completion evidence")
    completed = committed
    plans = [fact for fact in facts if fact.get("status") == "planned" and PLAN_INTENT.search(str(fact.get("text", "")))]
    plan_text = _evidence_lines(plans, {"planned"})
    summary = ""
    if ai_result is not None:
        completed = ai_result.get("completed", completed)
        summary = ai_result.get("summary", summary)
        plan_text = ai_result.get("plan", plan_text)
    if manual_text is not None and kind == "daily":
        summary = manual_text
    values = {names["completed"]: completed, names["summary"]: summary}
    if names.get("plan") and plan_text:
        values[names["plan"]] = plan_text
    if names.get("date"):
        values[names["date"]] = day
    if hours is not None:
        if kind != "daily":
            raise ValueError("hours only applies to daily reports")
        try:
            parsed = Decimal(str(hours))
        except InvalidOperation:
            raise ValueError("invalid hours") from None
        if not parsed.is_finite() or parsed < 0 or parsed > 24:
            raise ValueError("invalid hours")
        values[names["hours"]] = format(parsed, "f")
    fields = []
    period_start, period_end = period_bounds(kind, date.fromisoformat(day))
    china = ZoneInfo("Asia/Shanghai")
    start_label = period_start.astimezone(china).date().isoformat()
    end_label = (period_end - timedelta(microseconds=1)).astimezone(china).date().isoformat()
    values[names["completed"]] = f"统计周期：{start_label} 至 {end_label}\n" + values[names["completed"]]
    for field in template_fields(template):
        name = field["field_name"]
        if name in values:
            fields.append({"name": name, "type": int(field["field_type"]),
                           "sort": int(field["field_sort"]), "value": values[name]})
    if names["completed"] not in {f["name"] for f in fields}:
        blockers.append("required completion field missing from live template")
    if kind == "daily" and names["date"] not in {f["name"] for f in fields}:
        blockers.append("date field missing from live template")
    draft = {"kind": kind, "date": day, "template_id": template.get("report_template_id"),
             "template_name": template.get("report_template_name"),
             "template_fingerprint": template_fingerprint(template),
             "fields": fields, "evidence_ids": [f["evidence_id"] for f in facts],
             "blockers": blockers, "generation_engine": "codex" if ai_result is not None else "extractive",
             "warnings": evidence.get("warnings", [])}
    return freeze_draft(draft)
