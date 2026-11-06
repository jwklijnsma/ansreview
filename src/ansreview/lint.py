"""Run ansible-lint and convert its Code Climate JSON into Findings."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from .models import Finding, Severity

SEVERITY_MAP = {
    "blocker": Severity.CRITICAL,
    "critical": Severity.HIGH,
    "major": Severity.MEDIUM,
    "minor": Severity.LOW,
    "info": Severity.INFO,
}

SECURITY_RULES = {"no-log-password", "risky-file-permissions", "risky-shell-pipe",
                  "command-instead-of-shell", "partial-become", "no-same-owner"}
IDEMPOTENCY_RULES = {"no-changed-when", "no-handler", "package-latest", "latest", "git-latest"}


def available() -> str | None:
    # Prefer the ansible-lint installed next to this Python (same venv), even if
    # that venv's bin/ is not on PATH.
    sibling = Path(sys.executable).parent / "ansible-lint"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    return shutil.which("ansible-lint")


def version() -> str:
    exe = available()
    if not exe:
        return ""
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60)
        return out.stdout.splitlines()[0] if out.stdout else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _category(rule: str) -> str:
    base = rule.split("[")[0]
    if base in SECURITY_RULES:
        return "security"
    if base in IDEMPOTENCY_RULES:
        return "idempotency"
    if base in {"syntax-check", "load-failure", "parser-error", "internal-error"}:
        return "reliability"
    return "style"


def run_lint(root: Path, files: list[str] | None = None, profile: str = "",
             timeout: int = 900, offline: bool = True) -> tuple[list[Finding], list[str]]:
    exe = available()
    if not exe:
        return [], ["ansible-lint not found on PATH; skipped (pip install ansible-lint)"]

    cmd = [exe, "-f", "codeclimate", "--nocolor", "-q"] + (["--offline"] if offline else [])
    if profile:
        cmd += ["--profile", profile]
    if files:
        cmd += files

    try:
        proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return [], [f"ansible-lint timed out after {timeout}s"]

    stdout = proc.stdout.strip()
    if not stdout:
        if proc.returncode not in (0, 2):
            tail = (proc.stderr or "").strip().splitlines()[-5:]
            return [], ["ansible-lint failed: " + " | ".join(tail)]
        return [], []

    start = stdout.find("[")
    try:
        data = json.loads(stdout[start:]) if start >= 0 else []
    except json.JSONDecodeError as exc:
        return [], [f"could not parse ansible-lint output: {exc}"]

    findings = []
    for item in data:
        loc = item.get("location", {})
        line = None
        lines = loc.get("lines") or {}
        if isinstance(lines.get("begin"), int):
            line = lines["begin"]
        elif isinstance(lines.get("begin"), dict):
            line = lines["begin"].get("line")
        positions = loc.get("positions") or {}
        if line is None and isinstance(positions.get("begin"), dict):
            line = positions["begin"].get("line")
        rule = item.get("check_name", "ansible-lint")
        findings.append(Finding(
            rule=rule,
            severity=SEVERITY_MAP.get(str(item.get("severity", "")).lower(), Severity.LOW),
            file=loc.get("path", "?"),
            line=line,
            message=item.get("description", "").strip(),
            source="ansible-lint",
            category=_category(rule),
            suggestion=(item.get("url") or ""),
        ))
    return findings, []
