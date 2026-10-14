"""Portable entry point for an installed skill; does not invoke a shell."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--project")
    options, arguments = parser.parse_known_args()
    skill_root = Path(__file__).resolve().parents[1]
    mapping_path = skill_root / "runtime.local.json"
    mapping = json.loads(mapping_path.read_text(encoding="utf-8-sig")) if mapping_path.exists() else {}
    project = options.project or os.environ.get("LABLOG_PROJECT_ROOT") or mapping.get("project_root")
    if not project:
        candidate = skill_root.parents[1]
        if (candidate / "lablog" / "__main__.py").is_file():
            project = str(candidate)
    if not project or not (Path(project) / "lablog" / "__main__.py").is_file():
        print("未找到 LabLog-Agent；请指定 --project 或运行 scripts/install-skill.ps1。", file=sys.stderr)
        return 2
    if "--config" not in arguments and mapping.get("config"):
        arguments = ["--config", mapping["config"], *arguments]
    python = mapping.get("python") or sys.executable
    return subprocess.run([python, "-X", "utf8", "-m", "lablog", *arguments], cwd=project, shell=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
