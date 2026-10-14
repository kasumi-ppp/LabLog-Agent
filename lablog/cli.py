"""Command-line interface; only submit may mutate DingTalk."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .ai import synthesize, validate_ai_result
from .backlog import KIND_COMPLETED, KIND_PLAN, append as backlog_append, char_count, load as backlog_load, mark_consumed, normalize_kind, stats as backlog_stats, take as backlog_take
from .config import is_workday, load_config, resolve_path, schedule_due
from .dws import DwsBlocked, DwsClient, DwsNoReportId, encode_contents, template_fingerprint
from .notify import send as send_notification
from .periods import parse_period_marker, period_labels, format_period_marker
from .report import CITATION, FIELDS, build_draft
from .sources import collect_codex, collect_git, collect_markdown, period_bounds
from .workflow import SubmissionBlocked, atomic_json, atomic_text, draft_hash, file_lock, validate_submission

CHINA = ZoneInfo("Asia/Shanghai")


def _client(cfg):
    dws = cfg.get("dws") or {}
    return DwsClient(dws.get("executable"), dws.get("profile"), int(dws.get("timeout_seconds", 30)))


def _path(cfg, kind, day, folder):
    root = resolve_path(cfg, cfg.get("data_dir", "data"))
    return root / folder / f"{kind}-{day}.json"


def _period_identity(draft):
    start, end = period_bounds(draft["kind"], date.fromisoformat(draft["date"]))
    return "\0".join((str((draft.get("approval_context") or {}).get("profile")),
                       str(draft.get("template_id")), str(draft.get("kind")), start.isoformat(), end.isoformat()))


def _period_labels(kind, day):
    return period_labels(kind, day)


def _state_path(cfg, draft):
    state_key = hashlib.sha256(_period_identity(draft).encode()).hexdigest()
    return resolve_path(cfg, cfg.get("data_dir", "data")) / "state" / (state_key + ".json")


def _template_cfg(cfg, kind):
    template = (cfg.get("templates") or {}).get(kind) or {}
    if not template.get("id") or not template.get("name"):
        raise ValueError(f"template {kind} ID/name missing")
    return template


def _approval_context(cfg, kind, day):
    start, end = period_bounds(kind, date.fromisoformat(day))
    return {"profile": (cfg.get("dws") or {}).get("profile"),
            "identity": cfg.get("identity") or {},
            "recipients": cfg.get("recipients") or [],
            "template": _template_cfg(cfg, kind),
            "period_start": start.isoformat(), "period_end": end.isoformat(),
            "date_encoding": cfg.get("date_encoding") or {},
            "quality": cfg.get("quality") or {},
            "history_from": cfg.get("history_from")}


SCOPE_CODEX_KEYS = ("project_paths", "session_ids", "exclude_paths")
SCOPE_SUPPLEMENTAL_KEYS = ("git_paths", "markdown_paths")


def _apply_scope(cfg, scope_file):
    """Overlay an externally supplied evidence scope onto a copy of the config.

    This is the extension point for deciding *what* a run may look at without
    editing the config file: a person, Codex, or another agent writes a scope
    JSON and passes ``--scope-file``. Recognised keys:

        project_paths   allowlist of working directories for Codex sessions
        session_ids     allowlist of specific Codex conversation ids
        exclude_paths   directories to ignore even when allowlisted
        git_paths       git repositories to mine for commits
        markdown_paths  directories to mine for dated Markdown notes

    Any omitted key keeps the value from the config file. Supplying
    ``project_paths`` or ``session_ids`` switches Codex collection to
    ``selected`` mode, which is what makes those lists an allowlist.
    """
    if not scope_file:
        return cfg
    data = json.loads(Path(scope_file).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("scope file must be a JSON object")
    unknown = set(data) - set(SCOPE_CODEX_KEYS) - set(SCOPE_SUPPLEMENTAL_KEYS)
    if unknown:
        raise ValueError("unknown scope keys: " + ", ".join(sorted(unknown)))
    scoped = copy.deepcopy(cfg)
    codex = scoped.setdefault("codex", {})
    supplemental = scoped.setdefault("supplemental", {})
    for key in SCOPE_CODEX_KEYS:
        if key in data:
            codex[key] = _scope_list(data[key], key)
    for key in SCOPE_SUPPLEMENTAL_KEYS:
        if key in data:
            supplemental[key] = _scope_list(data[key], key)
    if "project_paths" in data or "session_ids" in data:
        codex["mode"] = "selected"
    scoped["scope_source"] = str(Path(scope_file).resolve())
    return scoped


def _scope_list(value, key):
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError(f"scope {key} must be a list of strings")
    return value


def _collect(cfg, kind, day):
    codex = cfg.get("codex") or {}
    if codex.get("mode", "auto_work") not in {"auto_work", "selected"}:
        raise ValueError("Codex mode must be auto_work or selected")
    if codex.get("mode") == "selected" and not (codex.get("session_ids") or codex.get("project_paths")):
        raise ValueError("selected mode requires project_paths or session_ids")
    paths = [resolve_path(cfg, x) for x in codex.get("session_roots", [])]
    primary = collect_codex(paths, kind, date.fromisoformat(day),
                            project_paths=codex.get("project_paths", []),
                            session_ids=codex.get("session_ids", []),
                            exclude_paths=codex.get("exclude_paths", []))
    supplemental = cfg.get("supplemental") or {}
    git = collect_git([resolve_path(cfg, x) for x in supplemental.get("git_paths", [])], kind, date.fromisoformat(day))
    md = collect_markdown([resolve_path(cfg, x) for x in supplemental.get("markdown_paths", [])], kind, date.fromisoformat(day))
    source_facts = primary["facts"] + git["facts"] + md["facts"]
    facts = _deduplicate_facts(source_facts)
    result = {"kind": kind, "date": day, "facts": facts,
              "source_fact_count": len(source_facts),
              "warnings": primary["warnings"] + git["warnings"] + md["warnings"]}
    path = _path(cfg, kind, day, "evidence")
    atomic_json(path, result)
    return path, result


def _deduplicate_facts(facts):
    """Merge exact normalized repeats while keeping status and provenance separate."""
    groups = {}
    confidence = {"high": 3, "medium": 2, "low": 1}
    for fact in facts:
        normalized = "".join(char for char in unicodedata.normalize("NFKC", str(fact.get("text", ""))).casefold()
                            if char.isalnum())
        if len(normalized) < 12:
            key = (normalized, fact.get("status"), fact.get("evidence_id"))
        else:
            key = (normalized, fact.get("status"))
        current = groups.get(key)
        provenance = {"evidence_id": fact.get("evidence_id"), "source": fact.get("source"),
                      "source_path": fact.get("source_path"), "status": fact.get("status"),
                      "time": fact.get("time")}
        if current is None:
            item = dict(fact)
            item["evidence_ids"] = [fact.get("evidence_id")] if fact.get("evidence_id") else []
            item["supporting_evidence"] = [provenance]
            groups[key] = item
            continue
        if fact.get("evidence_id") and fact["evidence_id"] not in current["evidence_ids"]:
            current["evidence_ids"].append(fact["evidence_id"])
        current["supporting_evidence"].append(provenance)
        existing_sources = current.setdefault("sources", [current.get("source")])
        if fact.get("source") not in existing_sources:
            existing_sources.append(fact.get("source"))
        if confidence.get(fact.get("confidence"), 0) > confidence.get(current.get("confidence"), 0):
            primary_id = current.get("evidence_id")
            item = dict(fact)
            item["evidence_id"] = primary_id
            item["evidence_ids"] = current["evidence_ids"]
            item["supporting_evidence"] = current["supporting_evidence"]
            item["sources"] = existing_sources
            groups[key] = item
    return list(groups.values())


def _carryover_cfg(cfg):
    """Return the carryover settings, always as a mapping."""
    return ((cfg.get("quality") or {}).get("carryover") or {})


def _carryover_enabled(cfg, kind):
    settings = _carryover_cfg(cfg)
    return bool(settings.get("enabled")) and kind == "daily"


def _strip_period_marker(value):
    """Split a daily field value into its ``统计周期：`` prefix and the body.

    The marker is machine metadata prepended by ``build_draft``; length budgets
    and truncation apply to the body only. Returns ``(prefix, body)`` where the
    prefix keeps its trailing newline so it can be re-attached verbatim.
    """
    if not isinstance(value, str):
        return "", ""
    lines = value.split("\n")
    if lines and lines[0].startswith("统计周期："):
        return lines[0] + "\n", "\n".join(lines[1:])
    return "", value


def _target_chars(settings):
    value = settings.get("target_chars", 200)
    if isinstance(value, bool) or not isinstance(value, int):
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ValueError("carryover target_chars must be an integer") from None
    if value <= 0:
        raise ValueError("carryover target_chars must be positive")
    return value


def _split_sentences(body):
    """Break a body into paragraph/sentence units without cutting mid-sentence.

    Paragraph breaks (``\\n\\n``) are hard boundaries; within a paragraph a
    sentence ends after Chinese or ASCII terminal punctuation followed by end of
    text, whitespace, or a closing quote. Every unit is returned verbatim so the
    kept prefix can be re-joined losslessly.
    """
    units = []
    for paragraph in body.split("\n\n"):
        if not paragraph.strip():
            continue
        for sentence in re.split(r"(?<=[。！？!?])[\s]*(?=\S)", paragraph):
            sentence = sentence.strip("\n\r")
            if sentence.strip():
                units.append(sentence)
    return units


def _cut_at_boundary(body, target_chars):
    """Keep whole leading sentences up to ``target_chars``; return (kept, rest).

    At least one sentence is always kept so an over-long first sentence is never
    truncated mid-way. ``rest`` is the sentences that did not fit, joined by
    newlines, and may be empty. This is the sentence-safe fallback used when no
    evidence-backed themes can be derived.
    """
    units = _split_sentences(body)
    kept, rest, total = [], [], 0
    for unit in units:
        size = char_count(unit)
        if kept and total + size > target_chars:
            rest.append(unit)
            continue
        kept.append(unit)
        total += size
    return "\n".join(kept), "\n".join(rest)


def _topic_keys(evidence):
    """Derive {topic_key: [keyword, ...]} from the evidence's real path fields.

    Themes are taken from fields the collectors actually populate, never
    invented: codex -> ``project``; git -> ``project`` / ``source_path``;
    markdown -> the parent directory of ``path``. Keywords are the last one or
    two path segments (longest first) so a sentence naming ``VR`` or
    ``project/VR`` matches. Returns an ordered mapping, empty when evidence is
    missing or shapeless.
    """
    keys = {}
    for fact in (evidence or {}).get("facts") or []:
        if not isinstance(fact, dict):
            continue
        topic = None
        if fact.get("source") == "markdown" and fact.get("path"):
            topic = str(Path(str(fact["path"])).parent)
        elif fact.get("project"):
            topic = str(fact["project"])
        elif fact.get("source_path"):
            topic = str(fact["source_path"])
        if not topic:
            continue
        parts = [p for p in re.split(r"[\\/]+", topic) if p]
        if not parts:
            continue
        keywords = keys.setdefault(topic, [])
        for candidate in (parts[-2] + "/" + parts[-1] if len(parts) >= 2 else None, parts[-1]):
            if candidate and candidate not in keywords:
                keywords.append(candidate)
    return keys


def _topic_of_sentence(sentence, topics):
    """Return the first topic key whose keyword appears in the sentence, or None."""
    folded = sentence.casefold()
    for topic in topics:
        if any(keyword.casefold() in folded for keyword in topics[topic]):
            return topic
    return None


def _theme_blocks(body, topics):
    """Group body sentences into contiguous theme blocks.

    A sentence naming no known topic joins the preceding block (so unrelated
    glue text is never orphaned into its own droppable block); when it is the
    very first sentence and matches nothing, a trailing block starts from it and
    later sentences may join once a theme is seen. Returns
    ``[(topic_or_None, [sentence, ...]), ...]`` in reading order.
    """
    blocks = []
    for sentence in _split_sentences(body):
        topic = _topic_of_sentence(sentence, topics)
        if blocks and (topic is None or topic == blocks[-1][0]):
            blocks[-1][1].append(sentence)
            continue
        blocks.append((topic, [sentence]))
    return blocks


def _cut_by_theme(body, target_chars, evidence):
    """Drop whole trailing theme blocks once the target length is reached.

    The dominant theme blocks (heaviest first) are kept until at least
    ``target_chars`` is covered, then the remaining *whole* blocks are queued so
    no theme is split across the report and the backlog. A single over-long
    block is always kept in full (returns ``(body, "")``). Returns ``(kept, rest)``
    or ``None`` when no themes can be derived, so the caller can fall back to
    sentence cutting.
    """
    topics = _topic_keys(evidence)
    if not topics:
        return None
    blocks = _theme_blocks(body, topics)
    if len(blocks) <= 1:
        # One theme only: keep it whole rather than telling the day's sole work
        # in fragments, even when it exceeds the target length.
        return body, ""
    sizes = [sum(char_count(sentence) for sentence in sentences) for _, sentences in blocks]
    # Keep the heaviest blocks first until the lower bound is met, but preserve
    # the original reading order for the kept text.
    order = sorted(range(len(blocks)), key=lambda i: (-sizes[i], i))
    keep_indexes, total = [], 0
    for index in order:
        keep_indexes.append(index)
        total += sizes[index]
        if total >= target_chars:
            break
    kept = [blocks[i] for i in sorted(keep_indexes)]
    rest = [blocks[i] for i in range(len(blocks)) if i not in set(keep_indexes)]
    kept_text = "\n".join(sentence for _, sentences in kept for sentence in sentences)
    rest_text = "\n".join(sentence for _, sentences in rest for sentence in sentences)
    return kept_text, rest_text


def _field_by_name(draft, name):
    for field in draft.get("fields") or []:
        if field.get("name") == name:
            return field
    return None


def _field_citation_ids(draft, name):
    return list((draft.get("citations") or {}).get(name) or [])


def _apply_carryover(cfg, kind, day, draft, evidence):
    """Trim over-long daily fields and queue the overflow into the backlog.

    The kept part is written back onto the draft field (period marker preserved)
    and the overflow becomes a backlog item, so a later day without fresh
    evidence can still submit real work. Trailing *whole* theme blocks are
    queued via ``_cut_by_theme``; a single over-long theme, or a body with no
    evidence-backed themes, keeps everything up to one sentence past the target
    via ``_cut_at_boundary``. Plan text is queued with ``kind="plan"`` so it is
    never substituted for a day's completed work. Returns a JSON-safe summary.
    """
    if not _carryover_enabled(cfg, kind):
        return {"status": "disabled"}
    settings = _carryover_cfg(cfg)
    target = _target_chars(settings)
    names = FIELDS.get("daily") or {}
    apply_to = settings.get("apply_to") or ["completed"]
    if not isinstance(apply_to, list):
        raise ValueError("carryover apply_to must be a list")
    field_names = []
    for key in apply_to:
        name = names.get(key) if key in names else None
        if name and name not in field_names:
            field_names.append(name)
    queued_items, kept_chars, queued_chars = [], 0, 0
    for name in field_names:
        field = _field_by_name(draft, name)
        if field is None or not isinstance(field.get("value"), str):
            continue
        prefix, body = _strip_period_marker(field["value"])
        if char_count(body) <= target:
            continue
        trimmed = _cut_by_theme(body, target, evidence)
        if trimmed is None:
            trimmed = _cut_at_boundary(body, target)
        kept, rest = trimmed
        field["value"] = prefix + kept
        kept_chars += char_count(kept)
        if not rest.strip():
            continue
        item = {"text": rest, "created_date": day,
                "source": draft.get("generation_engine", "unknown"),
                "evidence_ids": _field_citation_ids(draft, name),
                "kind": KIND_COMPLETED}
        added = backlog_append(cfg, [item])
        if added:
            queued_items.append(item_id(rest))
            queued_chars += char_count(rest)
    plan_name = names.get("plan")
    if settings.get("plan_extra", True) and plan_name:
        field = _field_by_name(draft, plan_name)
        if field is not None and isinstance(field.get("value"), str):
            prefix, body = _strip_period_marker(field["value"])
            if body.strip():
                item = {"text": body, "created_date": day,
                        "source": draft.get("generation_engine", "unknown"),
                        "evidence_ids": _field_citation_ids(draft, plan_name),
                        "kind": KIND_PLAN}
                if backlog_append(cfg, [item]):
                    queued_items.append(item_id(body))
                    queued_chars += char_count(body)
    result = {"target_chars": target, "kept_chars": kept_chars,
              "queued_items": queued_items, "queued_chars": queued_chars,
              "note": f"正文超过 {target} 字，超出部分已存入 backlog 供后续补交"}
    draft["carryover"] = {**(draft.get("carryover") or {}), **result}
    return {"status": "applied", **result}


def item_id(text):
    """Content hash used as the backlog item id (mirrors backlog.append)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]


def _fill_from_backlog(cfg, kind, day, draft, evidence):
    """Substitute pending backlog prose when a day has no fresh work evidence.

    Only runs for an otherwise blockable draft whose completion field is empty.
    Pulls ``kind == "completed"`` entries only (the ``backlog.take`` default), so
    plan text is never passed off as a day's completed work; the text is cleaned
    of any ``[证据:...]`` trace and joined with blank lines. Never marks the
    backlog consumed here -- that waits for a verified submit.
    """
    if not _carryover_enabled(cfg, kind):
        return {"status": "disabled"}
    blockers = draft.get("blockers") or []
    if "no dated completion evidence" not in blockers:
        return {"status": "skipped", "reason": "no missing-evidence blocker"}
    settings = _carryover_cfg(cfg)
    target = _target_chars(settings)
    name = (FIELDS.get("daily") or {}).get("completed")
    field = _field_by_name(draft, name) if name else None
    prefix, body = _strip_period_marker(field["value"] if field is not None else "")
    if body.strip():
        return {"status": "skipped", "reason": "completion field is not empty"}
    selected, total, _ = backlog_take(cfg, target, date=day, kind=KIND_COMPLETED)
    if not selected:
        return {"status": "skipped", "reason": "backlog is empty"}
    texts = []
    for entry in selected:
        cleaned = CITATION.sub("", str(entry.get("text", ""))).strip()
        if cleaned:
            texts.append(cleaned)
    if not texts:
        return {"status": "skipped", "reason": "backlog items are empty after cleaning"}
    filled = "\n\n".join(texts)
    if field is None:
        return {"status": "skipped", "reason": "completion field missing from the live template"}
    marker = prefix or (format_period_marker("daily", day) + "\n")
    field["value"] = marker + filled
    draft["blockers"] = [item for item in blockers if item != "no dated completion evidence"]
    days = sorted(str(entry.get("created_date")) for entry in selected if entry.get("created_date"))
    origin = days[0] if days else day
    warning = f"正文来自 backlog 补交（原始工作日期：{origin}）"
    warnings = draft.setdefault("warnings", [])
    if warning not in warnings:
        warnings.append(warning)
    summary = {"filled_items": [str(entry.get("id")) for entry in selected],
               "filled_chars": char_count(filled)}
    draft["carryover"] = {**(draft.get("carryover") or {}), **summary}
    return {"status": "filled", "origin_date": origin, **summary}


def _generate(cfg, kind, day, engine, manual_text_file=None, hours=None, ai_result_file=None):
    path = _path(cfg, kind, day, "evidence")
    # A previously collected snapshot may have been built under a different
    # project allowlist or may omit work added later in the same day.
    _, evidence = _collect(cfg, kind, day)
    template_cfg = _template_cfg(cfg, kind)
    template = _client(cfg).template(template_cfg["name"], template_cfg["id"])
    if engine == "codex":
        ai_result = synthesize(cfg, evidence, kind, day)
    elif engine == "assisted":
        if not ai_result_file:
            raise ValueError("assisted engine requires --ai-result-file")
        ai_result = validate_ai_result(json.loads(Path(ai_result_file).read_text(encoding="utf-8")), evidence.get("facts", []))
    else:
        ai_result = None
    manual = Path(manual_text_file).read_text(encoding="utf-8") if manual_text_file else None
    draft = build_draft(kind, day, template, evidence, manual_text=manual, hours=hours, ai_result=ai_result)
    if engine == "assisted":
        draft["generation_engine"] = "codex-assisted"
    draft["approval_context"] = _approval_context(cfg, kind, day)
    if kind == "daily" and hours is None and any(int(f["field_type"]) == 2 for f in template["report_template_fields"]):
        draft["warnings"].append("总工时/h 留空；未提供真实工时")
    if any(int(f["field_type"]) == 5 for f in template["report_template_fields"]):
        if not (cfg.get("date_encoding") or {}).get("verified"):
            draft["blockers"].append("date encoding unverified")
    draft["template_last_modified"] = template.get("lastModifiedTime")
    _apply_carryover(cfg, kind, day, draft, evidence)
    _fill_from_backlog(cfg, kind, day, draft, evidence)
    draft["hash"] = draft_hash(draft)
    draft_path = _path(cfg, kind, day, "drafts")
    with file_lock(draft_path):
        if draft_path.exists():
            prior = json.loads(draft_path.read_text(encoding="utf-8"))
            if prior.get("submission", {}).get("status") in {"submitting", "unknown", "verified", "submitted", "mismatch"}:
                raise SubmissionBlocked("existing draft cannot be replaced after submit attempt")
        atomic_json(draft_path, draft)
    return draft_path, draft


def _read_draft(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if draft_hash(data) != data.get("hash"):
        raise SubmissionBlocked("draft hash changed")
    return data


def _outbox_history(cfg, client, template_name, day, kind="daily"):
    earliest = cfg.get("history_from")
    if not earliest:
        raise SubmissionBlocked("history_from required for complete remote duplicate guard")
    current = datetime.combine(date.fromisoformat(earliest), datetime.min.time(), CHINA)
    finish = datetime.now(CHINA)
    report_start, _ = period_bounds(kind, date.fromisoformat(day))
    report_start = report_start.astimezone(CHINA)
    if current > report_start or current >= finish:
        raise SubmissionBlocked("history_from does not cover report period")
    all_reports = []
    while current < finish:
        next_end = min(current + timedelta(days=19), finish)
        all_reports.extend(client.outbox(template_name, current.isoformat(), next_end.isoformat()))
        current = next_end
    return all_reports


def _report_period(report, client, template_name):
    """Read a work-period marker from a fetched report; never use create time."""
    detail = None
    report_id = report.get("reportId") or report.get("report_id")
    if report_id:
        detail = client.detail(str(report_id))
    candidate = detail or report
    markers = []
    for key in ("work_period", "workPeriod", "period", "reportPeriod"):
        value = candidate.get(key)
        if isinstance(value, dict):
            start, end = value.get("start"), value.get("end")
            if start and end:
                markers.append((str(start)[:10], str(end)[:10]))
        elif isinstance(value, str):
            marker = parse_period_marker(value)
            if marker:
                markers.append(marker)
    contents = (candidate.get("contents") or candidate.get("report_entry_contents")
                or candidate.get("reportContents") or candidate.get("report_content"))
    if isinstance(contents, list):
        for item in contents:
            if not isinstance(item, dict):
                continue
            raw = item.get("content")
            if raw is None:
                raw = item.get("value")
            marker = parse_period_marker(raw)
            if marker:
                markers.append(marker)
            if item.get("key") == "工作日期" and isinstance(raw, str):
                try:
                    work_day = date.fromisoformat(raw.strip())
                    start, end = period_bounds("daily", work_day)
                    markers.append((start.astimezone(CHINA).date().isoformat(), (end - timedelta(microseconds=1)).astimezone(CHINA).date().isoformat()))
                except ValueError:
                    pass
    if "startDate" in candidate and "endDate" in candidate:
        markers.append((str(candidate["startDate"])[:10], str(candidate["endDate"])[:10]))
    unique = set(markers)
    if len(unique) == 1:
        return unique.pop()
    return None


def _has_period_duplicate(cfg, client, template_name, draft):
    reports = _outbox_history(cfg, client, template_name, draft["date"], draft["kind"])
    start, end = _period_labels(draft["kind"], draft["date"])
    for report in reports:
        if not isinstance(report, dict):
            raise SubmissionBlocked("outbox report item is ambiguous")
        period = _report_period(report, client, template_name)
        if period is None:
            raise SubmissionBlocked("an existing report has no verifiable work period; review before submit")
        if period == (start, end):
            return True
    return False


def _resolve_created_report(cfg, client, template_name, draft):
    """Resolve the report just created for this work period.

    The current DWS CLI answers ``entry submit`` with success but no report id,
    so the id has to be recovered from the outbox by work period.
    """
    try:
        reports = _outbox_history(cfg, client, template_name, draft["date"], draft["kind"])
    except (SubmissionBlocked, DwsBlocked, OSError, ValueError):
        return None
    wanted = _period_labels(draft["kind"], draft["date"])
    matches = []
    for report in reports:
        if not isinstance(report, dict):
            continue
        report_id = report.get("reportId") or report.get("report_id") or report.get("report_Id")
        if not report_id:
            continue
        try:
            period = _report_period(report, client, template_name)
        except (SubmissionBlocked, DwsBlocked, OSError, ValueError):
            continue
        if period == wanted:
            matches.append((report.get("createTime") or 0, str(report_id)))
    if not matches:
        return None
    matches.sort()
    return matches[-1][1]


def _verify_recipients(cfg, client):
    """Return validated recipient IDs, display results, and shared blockers."""
    recipients = cfg.get("recipients")
    if not isinstance(recipients, list) or not recipients:
        return [], [], ["recipient IDs missing"]
    identity = cfg.get("identity") or {}
    user_ids, results, blockers, seen = [], [], [], set()
    for recipient in recipients:
        recipient = recipient if isinstance(recipient, dict) else {}
        name, user_id = recipient.get("name"), recipient.get("user_id")
        issue = None
        if not user_id or user_id in seen or not name:
            issue = "recipient ID missing, duplicated or unlabeled"
        else:
            seen.add(user_id)
            contact = client.contact(user_id)
            expected_org = recipient.get("corp_name") or identity.get("corp_name")
            if contact.get("orgUserName") != name or contact.get("orgName") != expected_org:
                issue = "recipient identity mismatch"
        results.append({"name": name, "verified": issue is None, "reason": issue})
        if issue:
            blockers.append(issue)
        else:
            user_ids.append(user_id)
    return user_ids, results, blockers


def _preview_markdown(draft, context, duplicate):
    start, end = _period_labels(draft["kind"], draft["date"])
    identity = context.get("identity") or {}
    recipients = ", ".join(x.get("name", "") for x in context.get("recipients") or []) or "未配置"
    submission_status = (draft.get("submission") or {}).get("status") or "not_submitted"
    submission_label = "未提交" if submission_status == "not_submitted" else submission_status
    lines = [f"# {draft['kind']} {draft['date']}", "",
             f"工作周期: {start} 至 {end}",
             f"草稿 SHA-256: {draft['hash']}",
             f"提交状态: {submission_label}",
             f"组织/本人: {identity.get('corp_name', '')} / {identity.get('user_name', '')}",
             f"收件人: {recipients}",
             f"模板: {draft.get('template_name', '')}",
             f"查重: {duplicate.get('status', 'unknown')}（仅同工作周期）",
             f"查重创建时间范围: {duplicate.get('history_from') or '未知'} 至 {duplicate.get('checked_through') or '未知'}"]
    if duplicate.get("reason"):
        lines.append(f"查重说明: {duplicate['reason']}")
    lines.extend(["", f"模板指纹: {draft.get('template_fingerprint')}", ""])
    for field in draft["fields"]:
        lines.extend([f"## {field['name']}", "", str(field["value"]), ""])
    citations = draft.get("citations") or {}
    if citations:
        lines.extend(["## 证据来源（仅本地留档，不随日报发送）", ""])
        for name, ids in citations.items():
            lines.append(f"- {name}: " + "、".join(ids))
        lines.append("")
    for label, key in (("阻断项", "blockers"), ("提醒", "warnings")):
        lines.extend([f"## {label}", ""])
        items = draft.get(key) or []
        lines.extend(["- " + value for value in items] if items else ["- 无"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _preflight(cfg, draft, *, duplicate_check=True):
    if draft.get("blockers"):
        raise SubmissionBlocked("draft has unresolved blockers: " + "; ".join(draft["blockers"]))
    if draft.get("approval_context") != _approval_context(cfg, draft["kind"], draft["date"]):
        raise SubmissionBlocked("identity, recipients, template or encoding policy changed since approval")
    client = _client(cfg)
    auth = client.auth()
    identity = cfg.get("identity") or {}
    if not identity.get("corp_id") or not identity.get("user_id"):
        raise SubmissionBlocked("configured identity missing")
    if auth.get("corp_id") != identity["corp_id"] or auth.get("user_id") != identity["user_id"] or (identity.get("user_name") and auth.get("user_name") != identity["user_name"]):
        raise SubmissionBlocked("DWS identity mismatch")
    template_cfg = _template_cfg(cfg, draft["kind"])
    if draft.get("template_id") != template_cfg["id"]:
        raise SubmissionBlocked("configured template ID mismatch")
    template = client.template(template_cfg["name"], template_cfg["id"])
    if template_fingerprint(template) != draft.get("template_fingerprint"):
        raise SubmissionBlocked("template drift")
    user_ids, _, recipient_blockers = _verify_recipients(cfg, client)
    if recipient_blockers:
        raise SubmissionBlocked("; ".join(recipient_blockers))
    if template.get("allowAddReceivers") == 0:
        raise SubmissionBlocked("template disallows adding explicit recipients; receiver semantics unverified")
    if duplicate_check and _has_period_duplicate(cfg, client, template_cfg["name"], draft):
        raise SubmissionBlocked("remote report already exists for this template and period")
    contents = encode_contents(draft, template, cfg.get("date_encoding") or {})
    if not contents:
        raise SubmissionBlocked("no encodable report fields")
    return client, template, user_ids, contents


def _normalize_field_text(value):
    return str(value).replace("\r\n", "\n").strip()


def _readback_matches(detail, template_id, contents, report_id=None):
    if report_id:
        returned_id = (detail.get("reportId") or detail.get("report_id")
                       or detail.get("report_Id") or detail.get("id"))
        if returned_id and str(returned_id) != str(report_id):
            return False
    returned_template = (detail.get("report_template_id") or detail.get("templateId")
                         or detail.get("template_id"))
    if returned_template != template_id:
        return False
    returned = (detail.get("contents") or detail.get("report_entry_contents")
                or detail.get("reportContents") or detail.get("report_content"))
    if not isinstance(returned, list):
        return False
    by_key = {}
    for item in returned:
        if not isinstance(item, dict):
            return False
        key = item.get("key") or item.get("field_name")
        if not key or key in by_key:
            return False
        by_key[key] = item
    for expected in contents:
        item = by_key.get(expected["key"])
        if not item:
            return False
        value = item.get("content")
        if value is None:
            value = item.get("value")
        if value is None or _normalize_field_text(value) != _normalize_field_text(expected["content"]):
            return False
        returned_type = item.get("type", item.get("field_type"))
        returned_sort = item.get("sort", item.get("field_sort"))
        if returned_type is not None and str(returned_type) != str(expected["type"]):
            return False
        if returned_sort is not None and str(returned_sort) != str(expected["sort"]):
            return False
    return True


def _submit(cfg, path, confirm):
    path = Path(path)
    first = _read_draft(path)
    state_path = _state_path(cfg, first)
    with file_lock(path), file_lock(state_path):
        draft = _read_draft(path)
        validate_submission(draft, confirm, {"template_fingerprint": draft.get("template_fingerprint")})
        if state_path.exists():
            raise SubmissionBlocked("report period already has a local submission attempt")
        client, template, users, contents = _preflight(cfg, draft)
        draft["submission"] = {"status": "submitting", "attempted_at": datetime.now(CHINA).isoformat(),
                               "encoded_contents": contents}
        atomic_json(state_path, {"status": "submitting", "draft": str(path.resolve()), "hash": draft["hash"]})
        atomic_json(path, draft)
        try:
            try:
                report_id = client.submit(draft["template_id"], contents, users)
            except DwsNoReportId:
                report_id = _resolve_created_report(cfg, client, draft["template_name"], draft)
            if not report_id:
                raise SubmissionBlocked("submitted but the created report could not be resolved from the outbox")
            draft["submission"]["report_id"] = report_id
            detail = client.detail(report_id)
            if not _readback_matches(detail, draft["template_id"], contents, report_id):
                draft["submission"]["status"] = "mismatch"
                atomic_json(path, draft)
                atomic_json(state_path, {"status": "mismatch", "draft": str(path.resolve()), "report_id": report_id})
                raise SubmissionBlocked("readback field mismatch or unsupported detail shape")
            draft["submission"]["status"] = "verified"
            atomic_json(path, draft)
            atomic_json(state_path, {"status": "verified", "draft": str(path.resolve()), "report_id": report_id})
            # Only a verified submission consumes backlog prose. An unknown or
            # mismatched outcome must keep it so it can be resubmitted later.
            filled = (draft.get("carryover") or {}).get("filled_items") or []
            if filled:
                consumed = mark_consumed(cfg, filled, draft["date"], str(report_id))
                if consumed:
                    draft["carryover"]["consumed_items"] = consumed
                    atomic_json(path, draft)
        except BaseException as exc:
            if draft["submission"]["status"] == "submitting":
                draft["submission"]["status"] = "unknown"
                draft["submission"]["error"] = type(exc).__name__
                atomic_json(path, draft)
                atomic_json(state_path, {"status": "unknown", "draft": str(path.resolve()),
                                         "report_id": draft["submission"].get("report_id")})
            raise
    return {"status": draft["submission"]["status"], "report_id": draft["submission"]["report_id"],
            "backlog_consumed": (draft.get("carryover") or {}).get("consumed_items", 0), "draft": str(path)}


def _reconcile(cfg, path):
    path = Path(path)
    with file_lock(path):
        draft = _read_draft(path)
        status = draft.get("submission", {}).get("status")
        if status not in {"unknown", "mismatch", "submitting"}:
            return {"status": status or "not_submitted", "draft": str(path)}
        client = _client(cfg)
        submission = draft["submission"]
        report_id = submission.get("report_id")
        if draft.get("approval_context") != _approval_context(cfg, draft["kind"], draft["date"]):
            return {"status": "manual_review_required", "draft": str(path), "reason": "Identity or approval context changed since submission."}
        try:
            auth = client.auth()
        except DwsBlocked as exc:
            return {"status": "manual_review_required", "draft": str(path), "reason": str(exc)}
        identity = cfg.get("identity") or {}
        if auth.get("corp_id") != identity.get("corp_id") or auth.get("user_id") != identity.get("user_id"):
            return {"status": "manual_review_required", "draft": str(path), "reason": "Current account differs from the approved account."}
        if report_id:
            try:
                client, _, _, approved_contents = _preflight(cfg, draft, duplicate_check=False)
                detail = client.detail(report_id)
            except DwsBlocked as exc:
                return {"status": "manual_review_required", "draft": str(path), "reason": str(exc)}
            if _readback_matches(detail, draft["template_id"], approved_contents, report_id):
                submission["status"] = "verified"
                atomic_json(path, draft)
                state_path = _state_path(cfg, draft)
                with file_lock(state_path):
                    atomic_json(state_path, {"status": "verified", "draft": str(path.resolve()), "report_id": report_id})
                return {"status": "verified", "report_id": report_id, "draft": str(path)}
        reports = _outbox_history(cfg, client, draft["template_name"], draft["date"], draft["kind"])
        return {"status": "manual_review_required", "remote_count": len(reports), "draft": str(path),
                "reason": "Unknown attempts are never retried automatically; inspect remote report and reconcile manually."}


def _preview(cfg, path):
    draft = _read_draft(path)
    duplicate = {"status": "unknown"}
    try:
        found = _has_period_duplicate(cfg, _client(cfg), draft["template_name"], draft)
        duplicate = {"status": "possible_duplicate" if found else "none_found",
                     "history_from": cfg.get("history_from"), "period_only": True}
    except (SubmissionBlocked, DwsBlocked, OSError, ValueError) as exc:
        duplicate = {"status": "unknown", "reason": str(exc)}
    context = draft.get("approval_context") or {}
    period_start, period_end = _period_labels(draft["kind"], draft["date"])
    history_from = cfg.get("history_from")
    checked_through = datetime.now(CHINA).isoformat()
    preview_path = _path(cfg, draft["kind"], draft["date"], "previews")
    result = {"status": "preview", "draft": str(Path(path).resolve()), "hash": draft["hash"],
              "kind": draft["kind"], "date": draft["date"], "fields": draft["fields"],
              "blockers": draft.get("blockers", []), "warnings": draft.get("warnings", []),
              "identity": context.get("identity"), "recipients": context.get("recipients"),
              "profile": context.get("profile"), "template_fingerprint": draft.get("template_fingerprint"),
              "work_period": {"start": period_start, "end": period_end},
              "duplicate_check": {**duplicate, "history_from": history_from,
                                  "checked_through": checked_through, "work_period_only": True}}
    atomic_text(preview_path.with_suffix(".md"), _preview_markdown(draft, context, result["duplicate_check"]))
    result["preview_json"] = str(preview_path)
    result["preview_markdown"] = str(preview_path.with_suffix(".md"))
    atomic_json(preview_path, result)
    return result


NOTIFY_STATUSES = {"blocked", "error", "unknown", "mismatch"}


def _notify_outcome(cfg, outcome):
    """Mail the stop reason of an unattended run. Never masks the outcome."""
    if outcome.get("status") not in NOTIFY_STATUSES:
        return outcome
    lines = ["LabLog-Agent 自动执行未完成，需要你处理。", "",
             f"类型: {outcome['kind']}", f"日期: {outcome['date']}",
             f"结果: {outcome['status']}", f"原因: {outcome.get('reason', '')}", ""]
    if outcome.get("report_id"):
        lines += [f"已创建日志: {outcome['report_id']}", ""]
    for step in outcome.get("steps") or []:
        lines.append(f"- {step['step']}: {step.get('status', '')}")
    lines += ["", f"草稿: {outcome.get('draft') or '-'}",
              f"预览: {outcome.get('preview_markdown') or '-'}", "",
              "处理建议:",
              "- 认证失效(token_refresh_failed): 执行 dws auth login 重新扫码授权",
              "- 无完成证据(no dated completion evidence): 补当日工作记录后重跑",
              "- 查重异常/读回不一致: 先 dws report +outbox-list 人工核对，切勿重试提交"]
    try:
        outcome["notified"] = send_notification(
            cfg, f"[LabLog] 日报自动执行未完成 {outcome['date']} {outcome['status']}", "\n".join(lines))
    except Exception as exc:  # notification is a side channel; never break the run
        outcome["notified"] = {"status": "failed", "reason": type(exc).__name__}
    return outcome


def _auto(cfg, kind, day, engine, *, submit=False, manual_text_file=None, hours=None, ai_result_file=None,
          scope_file=None):
    """One-shot unattended pipeline: guards -> collect -> generate -> preview -> dry-run -> (submit).

    Every guard used by the interactive flow is reused here, so the unattended
    path is never weaker than a manual one. Unknown outcomes are never retried.
    ``scope_file`` overlays an externally chosen evidence scope for this run.
    """
    cfg = _apply_scope(cfg, scope_file)
    steps = []
    outcome = {"kind": kind, "date": day, "engine": engine, "steps": steps,
               "auto_submitted": False, "scope_source": cfg.get("scope_source"), "status": "ok"}
    draft_path = None

    def stop(status, reason, **extra):
        outcome.update({"status": status, "reason": reason})
        outcome.update(extra)
        return _notify_outcome(cfg, outcome)

    try:
        if kind == "daily" and not is_workday(cfg, date.fromisoformat(day)):
            steps.append({"step": "workday_gate", "status": "not_due"})
            outcome["status"] = "not_due"
            return outcome
        steps.append({"step": "workday_gate", "status": "due"})

        # Idempotency: a period that already has a local attempt must never be
        # re-generated (which would raise) and must never re-notify on success.
        existing = _path(cfg, kind, day, "drafts")
        if existing.exists():
            try:
                prior = json.loads(existing.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                prior = {}
            prior_status = (prior.get("submission") or {}).get("status")
            if prior_status in {"submitting", "unknown", "verified", "submitted", "mismatch"}:
                steps.append({"step": "idempotency", "status": prior_status})
                # unknown/mismatch still need a human, so they keep an alarming status
                # and mail; a verified period is simply done, and stays quiet.
                outcome.update({"status": prior_status if prior_status in {"unknown", "mismatch"} else "already_submitted",
                                "reason": f"this work period already has a local submission attempt ({prior_status})",
                                "draft": str(existing),
                                "report_id": (prior.get("submission") or {}).get("report_id")})
                return _notify_outcome(cfg, outcome)

        client = _client(cfg)
        try:
            auth = client.auth()
            identity = cfg.get("identity") or {}
            if (auth.get("corp_id") != identity.get("corp_id") or auth.get("user_id") != identity.get("user_id")
                    or (identity.get("user_name") and auth.get("user_name") != identity["user_name"])):
                raise DwsBlocked("DWS identity mismatch or authentication unavailable")
            steps.append({"step": "auth", "status": "ok"})

            _, recipient_results, recipient_blockers = _verify_recipients(cfg, client)
            if recipient_blockers:
                raise DwsBlocked("recipient verification failed: " + "; ".join(recipient_blockers))
            steps.append({"step": "recipients", "status": "ok", "recipients": recipient_results})

            if not (cfg.get("date_encoding") or {}).get("verified"):
                raise DwsBlocked("date encoding unverified for daily submission")
            if not cfg.get("history_from"):
                raise DwsBlocked("history_from missing for the duplicate guard")
        except (DwsBlocked, SubmissionBlocked) as exc:
            return stop("blocked", f"precondition failed: {exc}")

        draft_path, draft = _generate(cfg, kind, day, engine, manual_text_file, hours, ai_result_file)
        steps.append({"step": "generate", "status": "drafted", "hash": draft["hash"],
                      "blockers": list(draft.get("blockers") or [])})

        preview = _preview(cfg, draft_path)
        steps.append({"step": "preview", "status": "ok",
                      "duplicate_check": preview["duplicate_check"].get("status")})
        extra = {"draft": str(draft_path), "preview_markdown": preview.get("preview_markdown")}

        if draft.get("blockers"):
            return stop("blocked", "draft blockers: " + "; ".join(draft["blockers"]), **extra)
        duplicate_status = preview["duplicate_check"].get("status")
        if duplicate_status == "possible_duplicate":
            # The period already exists on the server: there is nothing to send,
            # and nothing a person has to act on. Staying quiet here is what makes
            # an every-workday schedule usable instead of a daily false alarm.
            steps.append({"step": "duplicate_gate", "status": "already_submitted"})
            outcome.update({"status": "already_submitted", "hash": draft["hash"],
                            "reason": "this work period already has a report on the server", **extra})
            return outcome
        if duplicate_status != "none_found":
            return stop("blocked", "remote duplicate check inconclusive: " + str(duplicate_status), **extra)

        try:
            _, _, users, contents = _preflight(cfg, draft)
            server_preview = client.submit(draft["template_id"], contents, users, dry_run=True)
        except (SubmissionBlocked, DwsBlocked) as exc:
            return stop("blocked", f"pre-flight or dry-run refused: {exc}", **extra)
        steps.append({"step": "dry_run", "status": "cli_dry_run", "recipient_count": len(users)})

        if not submit:
            outcome.update({"status": "ready", "hash": draft["hash"],
                            "server_preview": server_preview, **extra})
            return outcome

        result = _submit(cfg, draft_path, draft["hash"])
        steps.append({"step": "submit", "status": result["status"], "report_id": result.get("report_id")})
        outcome.update({"status": result["status"], "report_id": result.get("report_id"), **extra})
        outcome["auto_submitted"] = result["status"] == "verified"
        return _notify_outcome(cfg, outcome) if result["status"] != "verified" else outcome
    except (OSError, ValueError, KeyError, SubmissionBlocked, DwsBlocked) as exc:
        reason = f"{type(exc).__name__}: {exc}"
        # ``_submit`` records the terminal state before raising; surface it instead
        # of flattening a mismatch or an unknown outcome into a generic error.
        recorded = None
        if draft_path is not None:
            try:
                recorded = json.loads(Path(draft_path).read_text(encoding="utf-8")).get("submission", {}).get("status")
            except (OSError, ValueError):
                recorded = None
        status = recorded if recorded in {"unknown", "mismatch"} else "error"
        return stop(status, reason, draft=str(draft_path) if draft_path else None)


def _backlog_command(cfg, args):
    """Inspect and maintain the carryover backlog; --clear needs explicit --yes."""
    path = resolve_path(cfg, cfg.get("data_dir", "data")) / "backlog" / "items.json"
    if args.clear:
        if not args.yes:
            return {"status": "blocked", "reason": "backlog --clear requires --yes"}
        with file_lock(path):
            data = backlog_load(cfg)
            removed = len(data.get("items") or [])
            atomic_json(path, {"version": 1, "items": []})
        return {"status": "cleared", "removed": removed}
    if args.prune:
        with file_lock(path):
            data = backlog_load(cfg)
            kept = [entry for entry in data.get("items", []) if entry.get("status") != "consumed"]
            removed = len(data.get("items", [])) - len(kept)
            if removed:
                atomic_json(path, {"version": data.get("version", 1), "items": kept})
        return {"status": "pruned", "removed": removed}
    data = backlog_load(cfg)
    pending = [entry for entry in data.get("items", []) if entry.get("status") == "pending"]
    pending.sort(key=lambda entry: (str(entry.get("created_date", "")), str(entry.get("id", ""))))
    items = [{"id": entry.get("id"), "created_date": entry.get("created_date"),
              "kind": normalize_kind(entry.get("kind")), "chars": char_count(entry.get("text")),
              "preview": str(entry.get("text", ""))[:120]} for entry in pending]
    return {"status": "ok", "stats": backlog_stats(cfg), "items": items}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="lablog")
    parser.add_argument("--config", default="config/config.local.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    for command in ("collect", "generate", "scheduled"):
        item = sub.add_parser(command)
        item.add_argument("--kind", choices=("daily", "weekly", "monthly"), required=True)
        item.add_argument("--date", required=True)
        if command == "generate":
            item.add_argument("--engine", choices=("codex", "extractive", "assisted"), default="extractive")
            item.add_argument("--ai-result-file")
            item.add_argument("--manual-text-file")
            item.add_argument("--hours")
        if command in ("collect", "generate"):
            item.add_argument("--scope-file")
    auto = sub.add_parser("auto")
    auto.add_argument("--kind", choices=("daily", "weekly", "monthly"), default="daily")
    auto.add_argument("--date")
    auto.add_argument("--engine", choices=("codex", "extractive", "assisted"), default=None)
    auto.add_argument("--ai-result-file")
    auto.add_argument("--manual-text-file")
    auto.add_argument("--hours")
    auto.add_argument("--scope-file")
    auto.add_argument("--submit", action="store_true")
    backlog = sub.add_parser("backlog")
    backlog.add_argument("--show", action="store_true")
    backlog.add_argument("--prune", action="store_true")
    backlog.add_argument("--clear", action="store_true")
    backlog.add_argument("--yes", action="store_true")
    for command in ("preview", "dry-run", "reconcile", "submit"):
        item = sub.add_parser(command)
        item.add_argument("--draft", required=True)
        if command == "submit":
            item.add_argument("--confirm-hash", required=True)
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
        if args.command == "doctor":
            client = _client(cfg)
            auth = client.auth()
            identity = cfg.get("identity") or {}
            identity_ok = auth.get("corp_id") == identity.get("corp_id") and auth.get("user_id") == identity.get("user_id") and (not identity.get("user_name") or auth.get("user_name") == identity["user_name"])
            template_info = {}
            for kind, configured in (cfg.get("templates") or {}).items():
                live = client.template(configured["name"], configured["id"])
                template_info[kind] = {"fingerprint": template_fingerprint(live),
                                       "allow_add_receivers": live.get("allowAddReceivers")}
            _, recipient_results, recipient_blockers = _verify_recipients(cfg, client)
            blockers = []
            if not identity_ok:
                blockers.append("DWS identity mismatch")
            blockers.extend(recipient_blockers)
            if not (cfg.get("date_encoding") or {}).get("verified"):
                blockers.append("date encoding unverified for daily submission")
            if not cfg.get("history_from"):
                blockers.append("history_from missing for duplicate guard")
            result = {"status": "blocked" if blockers else "ok", "authenticated": True,
                      "identity_matches": identity_ok, "templates": template_info,
                      "recipients": recipient_results, "submission_blockers": blockers}
        elif args.command == "collect":
            path, evidence = _collect(_apply_scope(cfg, args.scope_file), args.kind, date.fromisoformat(args.date).isoformat())
            result = {"status": "collected", "path": str(path), "fact_count": len(evidence["facts"]), "warnings": evidence["warnings"]}
        elif args.command == "backlog":
            result = _backlog_command(cfg, args)
        elif args.command == "generate":
            path, draft = _generate(_apply_scope(cfg, args.scope_file), args.kind, date.fromisoformat(args.date).isoformat(), args.engine,
                                    args.manual_text_file, args.hours, args.ai_result_file)
            result = {"status": "drafted", "path": str(path), "hash": draft["hash"], "blockers": draft["blockers"], "engine": draft["generation_engine"]}
        elif args.command == "scheduled":
            day = date.fromisoformat(args.date)
            due = schedule_due(cfg, args.kind, day)
            if due:
                engine = (cfg.get("schedule") or {}).get("engine", "codex")
                if engine not in {"codex", "extractive"}:
                    raise ValueError("scheduled engine invalid")
                path, draft = _generate(cfg, args.kind, day.isoformat(), engine)
                result = {"status": "drafted", "path": str(path), "hash": draft["hash"], "auto_submitted": False}
            else:
                result = {"status": "not_due", "auto_submitted": False}
        elif args.command == "auto":
            day = date.fromisoformat(args.date).isoformat() if args.date else datetime.now(CHINA).date().isoformat()
            engine = (args.engine or (cfg.get("schedule") or {}).get("engine")
                      or (cfg.get("generation") or {}).get("engine") or "extractive")
            if engine not in {"codex", "extractive", "assisted"}:
                raise ValueError("auto engine invalid")
            result = _auto(cfg, args.kind, day, engine, submit=args.submit,
                           manual_text_file=args.manual_text_file, hours=args.hours,
                           ai_result_file=args.ai_result_file, scope_file=args.scope_file)
        elif args.command == "preview":
            result = _preview(cfg, args.draft)
        elif args.command == "dry-run":
            draft = _read_draft(args.draft)
            try:
                client, _, users, contents = _preflight(cfg, draft)
                cli_preview = client.submit(draft["template_id"], contents, users, dry_run=True)
                result = {"status": "cli_dry_run", "draft": str(Path(args.draft).resolve()),
                          "hash": draft["hash"], "recipient_count": len(users),
                          "server_preview": cli_preview}
            except (SubmissionBlocked, DwsBlocked) as exc:
                result = {"status": "blocked", "draft": str(Path(args.draft).resolve()),
                          "hash": draft["hash"], "reason": str(exc)}
        elif args.command == "submit":
            result = _submit(cfg, args.draft, args.confirm_hash)
        else:
            result = _reconcile(cfg, args.draft)
        print(json.dumps(result, ensure_ascii=True, default=str))
        failed = result.get("status") in {"blocked", "error", "unknown", "mismatch", "manual_review_required"}
        return 2 if failed else 0
    except (OSError, ValueError, KeyError, SubmissionBlocked, DwsBlocked) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=True), file=sys.stdout)
        return 2
