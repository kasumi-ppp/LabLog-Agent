"""Bounded native Codex synthesis with evidence citations."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["completed", "summary", "plan", "citations"],
          "properties": {"completed": {"type": "string"}, "summary": {"type": "string"},
                         "plan": {"type": "string"},
                         "citations": {"type": "array", "items": {"type": "string"}}}}
COMPLETION_CLAIM = re.compile(
    r"(?i)(?:已经?(?:完成|部署|上线|发布|修复|提交|实现)|完成(?:了)?(?:部署|上线|发布|修复|迁移|验证|开发|实现|提交)|"
    r"部署完成|上线完成|发布完成|修复完成|实现完成|提交成功|测试(?:全部)?通过|回归通过|"
    r"implemented|fixed|completed|deployed|tests pass(?:ed)?|successfully implemented)"
)
CHINESE_RUN = re.compile(r"[\u3400-\u9fff]+")
ENGLISH_TOKEN = re.compile(r"[a-z0-9]{3,}")
CHINESE_STOPWORDS = {"完成", "项目", "工作", "系统", "报告", "内容", "实现", "方法", "用户", "测试"}


def _claim_features(text):
    value = re.sub(r"\[证据:[^\]]+\]", "", str(text)).casefold()
    chinese = set()
    for run in CHINESE_RUN.findall(value):
        chinese.update(run[i:i + 2] for i in range(len(run) - 1))
    return chinese - CHINESE_STOPWORDS, set(ENGLISH_TOKEN.findall(value))


def _has_lexical_support(claim, source):
    claim_chinese, claim_english = _claim_features(claim)
    source_chinese, source_english = _claim_features(source)
    if claim_chinese & source_chinese or claim_english & source_english:
        return True
    if claim_chinese and source_chinese:
        return False
    if claim_english and source_english:
        return False
    # Different-language paraphrases need a person to assess semantic support in preview.
    return True


def validate_ai_result(result, facts):
    allowed = {x["evidence_id"] for x in facts}
    if not isinstance(result, dict) or set(result) != {"completed", "summary", "plan", "citations"}:
        raise ValueError("Codex output schema mismatch")
    if not all(isinstance(result[k], str) for k in ("completed", "summary", "plan")):
        raise ValueError("Codex output text invalid")
    if not isinstance(result["citations"], list) or not result["citations"] or not all(isinstance(x, str) for x in result["citations"]) or not set(result["citations"]) <= allowed:
        raise ValueError("Codex citations missing or unknown")
    text = result["completed"] + "\n" + result["summary"] + "\n" + result["plan"]
    if not all("[证据:" + cid + "]" in text for cid in result["citations"]):
        raise ValueError("Codex citations absent from prose")
    fact_by_id = {x["evidence_id"]: x for x in facts}
    for line in text.splitlines():
        if not line.strip():
            continue
        cited = re.findall(r"\[证据:([^\]]+)\]", line)
        if not cited or not set(cited) <= allowed:
            raise ValueError("Codex claim lacks known source citation")
        prose = re.sub(r"\[证据:[^\]]+\]", "", line)
        source_text = " ".join(str(fact_by_id[c].get("text", "")) for c in cited)
        if any(not _has_lexical_support(prose, str(fact_by_id[c].get("text", ""))) for c in cited):
            raise ValueError("Codex claim has no lexical support in its cited evidence")
        if not set(re.findall(r"\d+(?:\.\d+)?", prose)) <= set(re.findall(r"\d+(?:\.\d+)?", source_text)):
            raise ValueError("Codex introduced unsupported numbers")
    for line in result["completed"].splitlines():
        cited = re.findall(r"\[证据:([^\]]+)\]", line)
        if cited and any(fact_by_id[c].get("status") != "completed" for c in cited):
            raise ValueError("Codex completion claim has no completed evidence")
    for line in result["plan"].splitlines():
        cited = re.findall(r"\[证据:([^\]]+)\]", line)
        if cited and any(fact_by_id[c].get("status") != "planned" for c in cited):
            raise ValueError("Codex plan claim has no planned evidence")
    for line in result["summary"].splitlines():
        cited = re.findall(r"\[证据:([^\]]+)\]", line)
        if not cited:
            continue
        prose = re.sub(r"\[证据:[^\]]+\]", "", line)
        if COMPLETION_CLAIM.search(prose) and any(fact_by_id[c].get("status") != "completed" for c in cited):
            raise ValueError("Codex summary cannot present planned evidence as completed")
    return result


def synthesize(config, evidence, kind, day):
    cfg = config.get("codex") or {}
    executable = cfg.get("executable")
    if not executable:
        raise ValueError("Codex executable missing")
    facts = evidence.get("facts", [])[:100]
    if not facts:
        raise ValueError("no evidence for AI synthesis")
    payload = {"kind": kind, "date": day, "facts": facts}
    prompt = ("Create concise Chinese report text only from the JSON evidence below. "
              "Return completed, summary, plan, citations as JSON. Each claim must cite an evidence_id in the text "
              "using [证据:ID]. Every claim must share concrete terms with the cited evidence unless it is clearly a cross-language paraphrase. User requests and plans are not completed work. Stated completion is unverified. "
              "Never invent hours, attendance, dates, numbers, files, or completed outcomes. "
              "Do not quote private messages verbatim.\n" + json.dumps(payload, ensure_ascii=False))
    with tempfile.TemporaryDirectory() as directory:
        schema = Path(directory) / "schema.json"
        output = Path(directory) / "output.json"
        schema.write_text(json.dumps(SCHEMA), encoding="utf-8")
        argv = [str(executable), "exec", "--sandbox", "read-only", "--ephemeral",
                "--output-schema", str(schema), "--output-last-message", str(output),
                "--cd", str(config["_base"]), "--skip-git-repo-check", "-"]
        try:
            proc = subprocess.run(argv, input=prompt, text=True, encoding="utf-8", errors="replace", capture_output=True,
                                  timeout=int(cfg.get("timeout_seconds", 120)), shell=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(f"Codex synthesis unavailable: {type(exc).__name__}") from exc
        if proc.returncode or not output.is_file():
            from .config import resolve_path
            from .workflow import atomic_json
            log_dir = resolve_path(config, config.get("data_dir", "data")) / "diagnostics"
            log_path = log_dir / ("ai-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
            atomic_json(log_path, {"returncode": proc.returncode, "output_present": output.is_file(),
                                   "stderr": (proc.stderr or "")[:20000], "stdout": (proc.stdout or "")[:20000]})
            raise ValueError(f"Codex synthesis failed (exit {proc.returncode}); private diagnostics: {log_path}")
        try:
            result = json.loads(output.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Codex output is not JSON") from exc
    return validate_ai_result(result, facts)
