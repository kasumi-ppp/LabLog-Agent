"""Conservative mapping of evidence into the three existing templates."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re

from .dws import template_fields, template_fingerprint
from .periods import format_period_marker
from .workflow import freeze_draft

FIELDS = {
    "daily": {"completed": "今日完成", "summary": "心得体会", "plan": "明日计划", "coordination": "需协调工作", "date": "工作日期", "hours": "总工时/h"},
    "weekly": {"completed": "本周完成工作", "summary": "本周工作总结", "plan": "下周工作计划", "coordination": "需协调与帮助"},
    "monthly": {"completed": "本月工作内容", "summary": "本月工作总结", "plan": "下月工作计划", "coordination": "需帮助与支持"},
}
PLAN_INTENT = re.compile(r"(?i)(?:明天|下周|下月|计划|打算|准备|待办|todo|to-do|next\s+(?:day|week|month))")
CITATION = re.compile(r"\s*\[证据:([^\]]+)\]")
UNVERIFIED_NOTE = re.compile(r"[（(]\s*会话报告[，,]\s*未独立核验\s*[）)]")


def reader_text(value):
    """Strip machine-readable traceability marks from text a person will read.

    A submitted report is read by colleagues, so the body must be prose rather
    than a citation list. Traceability is kept instead in ``draft["citations"]``
    plus ``draft["evidence_ids"]`` and shown in the local preview.
    """
    if not isinstance(value, str):
        return value
    text = UNVERIFIED_NOTE.sub("", CITATION.sub("", value))
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line.strip() not in {"", "-", "*"})


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
        qualifier = "（会话报告，未独立核验）" if fact.get("source") in {"codex", "workbuddy", "chatgpt"} and fact.get("status") == "completed" else ""
        lines.append(f"- {text}{qualifier} [证据:{fact['evidence_id']}]")
    return "\n".join(lines)


def has_completion_body(value):
    text = reader_text(str(value or ''))
    text = re.sub(r'(?m)^\s*统计周期\s*[：:][^\n]*(?:\n|$)', '', text)
    return bool(text.strip(' \t\r\n-*•#>'))


def build_draft(kind, day, template, evidence, *, manual_text=None, hours=None, ai_result=None):
    if kind not in FIELDS:
        raise ValueError("unknown report kind")
    names = FIELDS[kind]
    facts = evidence.get("facts", [])
    committed = _evidence_lines(facts, {"completed", "committed"})
    blockers = list(evidence.get('blockers') or [])
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
    extras = (ai_result or {}).get('extra_fields', {})
    live_text_names = {f['field_name'] for f in template_fields(template) if int(f['field_type']) == 1}
    if set(extras) - live_text_names or set(extras) & set(names.values()):
        # Coordination is a core mapping but has no other generated value.
        forbidden = set(extras) & (set(names.values()) - {names['coordination']})
        if set(extras) - live_text_names or forbidden:
            raise ValueError('extra_fields cannot override core or unknown template fields')
    values.update(extras)
    fields = []
    citations = {}
    for name in values:
        if not name or not isinstance(values.get(name), str):
            continue
        found = sorted(set(CITATION.findall(values[name])))
        if not found:
            continue  # verbatim manual text must stay untouched
        citations[name] = found
        values[name] = reader_text(values[name])
    values[names["completed"]] = format_period_marker(kind, day) + "\n" + values[names["completed"]]
    if not has_completion_body(values[names['completed']]) and 'no dated completion evidence' not in blockers:
        blockers.append('empty completion body')
    for field in template_fields(template):
        name = field["field_name"]
        value = values.get(name)
        if any(field.get(k) in (True, 1, 'true') for k in ('required', 'is_required', 'isRequired')) and not value:
            blockers.append(f'required field missing: {name}')
        for keys, direction in ((('minLength', 'min_length'), 'min'), (('maxLength', 'max_length'), 'max')):
            for key in keys:
                limit = field.get(key)
                if limit is not None and value is not None:
                    try:
                        violated = len(str(value)) < int(limit) if direction == 'min' else len(str(value)) > int(limit)
                    except (ValueError, TypeError):
                        blockers.append(f'unrecognized template constraint {name}: {key}')
                    else:
                        if violated:
                            blockers.append(f'template constraint {name}: {key}={limit}')
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
             "citations": citations,
             "blockers": blockers, "generation_engine": "codex" if ai_result is not None else "extractive",
             "warnings": evidence.get("warnings", [])}
    return freeze_draft(draft)
