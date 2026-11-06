"""Core data types shared by every stage of the pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import IntEnum
import hashlib


class Severity(IntEnum):
    INFO = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    @classmethod
    def parse(cls, value: str | int | None, default: "Severity" = None) -> "Severity":
        if isinstance(value, Severity):
            return value
        if isinstance(value, int):
            return cls(max(0, min(4, value)))
        if not value:
            return default if default is not None else cls.MEDIUM
        key = str(value).strip().upper()
        aliases = {
            "BLOCKER": "CRITICAL", "MAJOR": "HIGH", "MINOR": "LOW",
            "WARNING": "MEDIUM", "WARN": "MEDIUM", "ERROR": "HIGH",
            "NOTE": "INFO", "HINT": "INFO", "MODERATE": "MEDIUM",
            "INFORMATIONAL": "INFO",
        }
        key = aliases.get(key, key)
        try:
            return cls[key]
        except KeyError:
            return default if default is not None else cls.MEDIUM

    @property
    def label(self) -> str:
        return self.name.lower()


@dataclass
class Finding:
    rule: str
    severity: Severity
    file: str            # path relative to the repo root
    line: int | None
    message: str
    source: str          # "ansible-lint", "static" or "llm"
    category: str = "general"   # security, reliability, idempotency, style, ...
    suggestion: str = ""
    confidence: float | None = None
    end_line: int | None = None   # last line of the task/mapping the finding is about

    @property
    def fingerprint(self) -> str:
        raw = f"{self.rule}|{self.file}|{self.line}|{self.message}"
        return hashlib.sha1(raw.encode()).hexdigest()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["severity"] = self.severity.label
        d["fingerprint"] = self.fingerprint
        return d


@dataclass
class Unit:
    """A reviewable chunk of the codebase: a role, a playbook, or a vars bundle."""
    kind: str                       # "role", "playbook", "vars", "other"
    name: str
    files: list[str] = field(default_factory=list)   # relative paths


@dataclass
class ScanResult:
    root: str
    units: list[Unit]
    findings: list[Finding]
    model: str | None = None
    errors: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        out = {s.label: 0 for s in Severity}
        for f in self.findings:
            out[f.severity.label] += 1
        return out

    def worst(self) -> Severity | None:
        return max((f.severity for f in self.findings), default=None)
