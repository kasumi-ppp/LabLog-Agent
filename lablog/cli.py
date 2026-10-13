"""Command-line interface; only submit may mutate DingTalk."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .ai import synthesize, validate_ai_result
from .config import load_config, resolve_path, schedule_due
from .dws import DwsBlocked, DwsClient, encode_contents, template_fingerprint
from .report import build_draft
from .sources import collect_codex, collect_git, collect_markdown, period_bounds
from .periods import parse_period_marker, period_labels
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
    contents = candidate.get("contents") or candidate.get("report_entry_contents") or candidate.get("reportContents")
    if isinstance(contents, list):
        for item in contents:
            if not isinstance(item, dict):
                continue
            marker = parse_period_marker(item.get("content"))
            if marker:
                markers.append(marker)
            if item.get("key") == "工作日期" and isinstance(item.get("content"), str):
                try:
                    work_day = date.fromisoformat(item["content"])
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


def _readback_matches(detail, template_id, contents, report_id=None):
    if report_id:
        returned_id = detail.get("reportId") or detail.get("report_id") or detail.get("id")
        if returned_id and str(returned_id) != str(report_id):
            return False
    if detail.get("report_template_id") != template_id and detail.get("templateId") != template_id:
        return False
    returned = detail.get("contents") or detail.get("report_entry_contents") or detail.get("reportContents")
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
        if not item or str(item.get("content")) != expected["content"]:
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
            report_id = client.submit(draft["template_id"], contents, users)
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
        except BaseException as exc:
            if draft["submission"]["status"] == "submitting":
                draft["submission"]["status"] = "unknown"
                draft["submission"]["error"] = type(exc).__name__
                atomic_json(path, draft)
                atomic_json(state_path, {"status": "unknown", "draft": str(path.resolve()),
                                         "report_id": draft["submission"].get("report_id")})
            raise
    return {"status": draft["submission"]["status"], "report_id": draft["submission"]["report_id"], "draft": str(path)}


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
            path, evidence = _collect(cfg, args.kind, date.fromisoformat(args.date).isoformat())
            result = {"status": "collected", "path": str(path), "fact_count": len(evidence["facts"]), "warnings": evidence["warnings"]}
        elif args.command == "generate":
            path, draft = _generate(cfg, args.kind, date.fromisoformat(args.date).isoformat(), args.engine,
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
        elif args.command == "preview":
            draft = _read_draft(args.draft)
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
            result = {"status": "preview", "draft": str(Path(args.draft).resolve()), "hash": draft["hash"],
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
        return 0 if result.get("status") not in {"blocked", "manual_review_required"} else 2
    except (OSError, ValueError, KeyError, SubmissionBlocked, DwsBlocked) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=True), file=sys.stdout)
        return 2
