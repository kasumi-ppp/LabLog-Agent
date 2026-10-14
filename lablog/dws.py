"""Guarded adapter for the official DingTalk Workspace CLI."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from decimal import Decimal, InvalidOperation


class DwsBlocked(Exception):
    pass


class DwsNoReportId(DwsBlocked):
    """``entry submit`` succeeded but the CLI response carried no report id."""

    def __init__(self, message="DWS submit returned no report ID; outcome unknown"):
        super().__init__(message)


def template_fields(template):
    fields = template.get("report_template_fields")
    if not isinstance(fields, list) or not fields:
        raise DwsBlocked("template fields unavailable")
    normalized = sorted(fields, key=lambda f: (int(f["field_sort"]), str(f["field_name"])))
    sorts = [int(f["field_sort"]) for f in normalized]
    names = [str(f["field_name"]) for f in normalized]
    if len(set(sorts)) != len(sorts) or len(set(names)) != len(names):
        raise DwsBlocked("ambiguous template fields")
    return normalized


def template_fingerprint(template):
    stable = {"id": template.get("report_template_id"),
              "modified": template.get("lastModifiedTime"),
              "allowAddReceivers": template.get("allowAddReceivers"),
              "fields": template_fields(template)}
    raw = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def encode_contents(draft, template, date_encoding):
    """Convert typed draft values only where the DWS representation is known."""
    provided = {f["name"]: f for f in draft.get("fields", [])}
    known = {f["field_name"] for f in template_fields(template)}
    if set(provided) - known:
        raise DwsBlocked("draft contains fields missing from live template")
    result = []
    for field in template_fields(template):
        name, field_type, sort = field["field_name"], int(field["field_type"]), int(field["field_sort"])
        if name not in provided:
            continue
        value = provided[name].get("value")
        if value is None or value == "":
            continue
        if int(provided[name].get("type", field_type)) != field_type:
            raise DwsBlocked("draft field type does not match template")
        if field_type == 1:
            if not isinstance(value, str):
                raise DwsBlocked("text field must be text")
            encoded = value
        elif field_type == 2:
            try:
                number = Decimal(str(value))
            except (InvalidOperation, TypeError):
                raise DwsBlocked("numeric field is invalid") from None
            if not number.is_finite() or number < 0:
                raise DwsBlocked("numeric field is invalid")
            encoded = format(number, "f")
        elif field_type == 5:
            if not date_encoding.get("verified") or date_encoding.get("format") != "iso-date":
                raise DwsBlocked("date encoding unverified")
            from datetime import date
            try:
                encoded = date.fromisoformat(str(value)).isoformat()
            except ValueError:
                raise DwsBlocked("date value invalid") from None
        else:
            raise DwsBlocked(f"field encoding for type {field_type} is unverified")
        result.append({"key": name, "sort": str(sort), "type": str(field_type),
                       "content": encoded, "contentType": "markdown" if field_type == 1 else "origin"})
    return result


def reports_success(data):
    """Accept every success envelope the DWS CLI has used for Report leaves.

    Atomic leaves answer ``{"success": true, "result": {...}}`` (no ``errcode``);
    legacy/other leaves answer ``{"errcode": 0, "success": true, ...}``; some
    helpers answer ``{"ok": true, "outcome": "success", ...}``.
    """
    if not isinstance(data, dict):
        return False
    if data.get("success") is True:
        return True
    if data.get("errcode") == 0:
        return True
    if data.get("ok") is True and data.get("outcome") == "success":
        return True
    return False


_REPORT_ID_KEYS = ("reportId", "report_id", "report_Id", "id")
_WRAPPER_KEYS = ("result", "data", "report", "reports")
_REPORT_ID_RE = re.compile(r"^[0-9a-zA-Z_-]{16,64}$")
_URL_ID_RE = re.compile(r"[?&]id=([0-9a-zA-Z_-]+)")


def extract_report_id(data):
    """Find the created report id across the envelopes DWS has used."""
    def visit(node, depth=0):
        if depth > 5:
            return None
        if isinstance(node, str):
            return node.strip() if _REPORT_ID_RE.match(node.strip()) else None
        if isinstance(node, list):
            for item in node:
                found = visit(item, depth + 1)
                if found:
                    return found
            return None
        if isinstance(node, dict):
            for key in _REPORT_ID_KEYS:
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for key in _WRAPPER_KEYS:
                if key in node:
                    found = visit(node[key], depth + 1)
                    if found:
                        return found
        return None

    found = visit({key: data[key] for key in _WRAPPER_KEYS if key in data})
    if found:
        return found
    for key in ("dingtalkOpenUrl", "dingtalkOpenMarkdownLink"):
        value = data.get(key)
        if isinstance(value, str):
            match = _URL_ID_RE.search(value)
            if match:
                return match.group(1)
    return None


class DwsClient:
    def __init__(self, executable, profile, timeout=30):
        if not executable or not profile:
            raise DwsBlocked("DWS executable and profile required")
        self.executable, self.profile, self.timeout = str(executable), str(profile), timeout

    def call(self, parts, *, stdin=None, mutating=False, dry_run=False):
        argv = [self.executable, "--profile", self.profile, "--format", "json"]
        if dry_run:
            argv.append("--dry-run")
        argv.extend(parts)
        try:
            proc = subprocess.run(argv, input=stdin, text=True, encoding="utf-8", errors="replace", capture_output=True,
                                  timeout=self.timeout, shell=False)
        except subprocess.TimeoutExpired as exc:
            raise DwsBlocked("DWS timeout; outcome unknown" if mutating else "DWS timeout") from exc
        except OSError as exc:
            raise DwsBlocked(f"DWS unavailable: {type(exc).__name__}") from exc
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise DwsBlocked("DWS returned non-JSON response" + ("; outcome unknown" if mutating else "")) from exc
        if proc.returncode != 0:
            raise DwsBlocked("DWS nonzero exit" + ("; outcome unknown" if mutating else ""))
        return data

    def auth(self):
        result = self.call(["auth", "status", "--readonly"])
        if result.get("success") is not True or result.get("authenticated") is not True or result.get("token_valid") is not True:
            raise DwsBlocked("DWS authentication unavailable")
        return result

    def template(self, template_name, template_id):
        data = self.call(["report", "template", "get", "--name", template_name])
        if data.get("errcode") != 0 or data.get("success") is not True:
            raise DwsBlocked("DWS template lookup failed")
        result = data.get("result") or {}
        if result.get("report_template_id") != template_id:
            raise DwsBlocked("DWS template ID mismatch")
        template_fields(result)
        return result

    def contact(self, user_id):
        data = self.call(["contact", "user", "get", "--ids", user_id])
        result = data.get("result")
        if data.get("success") is not True or not isinstance(result, (list, dict)):
            raise DwsBlocked("recipient lookup failed")
        items = result if isinstance(result, list) else [result]
        matches = [item.get("orgEmployeeModel", {}) for item in items
                   if item.get("orgEmployeeModel", {}).get("orgUserId") == user_id]
        if len(matches) != 1:
            raise DwsBlocked("recipient ID not uniquely verified")
        return matches[0]

    def outbox(self, template_name, start, end):
        reports, cursor = [], "0"
        for _ in range(50):
            data = self.call(["report", "+outbox-list", "--start", start, "--end", end,
                              "--template-name", template_name, "--cursor", cursor, "--size", "20"])
            if data.get("ok") is not True or data.get("outcome") != "success":
                raise DwsBlocked("outbox query failed")
            body = data.get("data") or {}
            page = body.get("reports")
            if not isinstance(page, list):
                raise DwsBlocked("outbox response incomplete")
            reports.extend(page)
            meta = data.get("meta") or {}
            pagination = meta.get("pagination") or {}
            if body.get("complete") is True and pagination.get("endpoint_exhausted") is True:
                return reports
            next_cursor = pagination.get("next_cursor") or pagination.get("nextCursor")
            if not next_cursor or str(next_cursor) == cursor:
                raise DwsBlocked("outbox pagination incomplete")
            cursor = str(next_cursor)
        raise DwsBlocked("outbox pagination limit reached")

    def submit(self, template_id, contents, user_ids, *, dry_run=False):
        data = self.call(["report", "entry", "submit", "--template-id", template_id,
                          "--contents", "-", "--to-user-ids", ",".join(user_ids)],
                         stdin=json.dumps(contents, ensure_ascii=False), mutating=not dry_run, dry_run=dry_run)
        if dry_run:
            if data.get("dry_run") is not True or data.get("executed") is not False or data.get("tool") != "create_report":
                raise DwsBlocked("DWS dry-run response does not prove nonexecution")
            return data
        if not reports_success(data):
            raise DwsBlocked("DWS submit failed; outcome unknown")
        report_id = extract_report_id(data)
        # The current CLI answers success without a reportId ("create response did
        # not include reportId"); callers resolve the id from the outbox instead.
        if not isinstance(report_id, str) or not report_id.strip():
            raise DwsNoReportId()
        return report_id

    def detail(self, report_id):
        data = self.call(["report", "entry", "get", "--report-id", report_id])
        if not reports_success(data):
            raise DwsBlocked("DWS detail readback failed")
        return data.get("result") or data.get("data") or {}
