"""Configuration: defaults < ~/.ansreview.yml < repo .ansreview.yml < env < CLI flags."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
import os
from pathlib import Path

import yaml

from .models import Severity

CONFIG_NAMES = (".ansreview.yml", ".ansreview.yaml")

# Keys a repo-level config is NOT allowed to set unless the user opts in,
# so a cloned repo cannot point your review at someone else's server.
REPO_UNTRUSTED_KEYS = {"ollama_host", "api_key"}


@dataclass
class Config:
    ollama_host: str = "http://localhost:11434"
    model: str = "qwen2.5-coder:14b"
    api_key: str = ""                 # for an Ollama behind an auth proxy
    language: str = "en"              # "en" or "nl"
    temperature: float = 0.1
    num_ctx: int = 16384
    timeout: int = 600                # seconds per LLM request
    retries: int = 2
    workers: int = 1                  # parallel LLM requests (1 = one GPU)
    max_chars_per_unit: int = 40000   # split a unit when its source exceeds this
    max_file_chars: int = 20000       # truncate single huge files
    use_llm: bool = True
    use_lint: bool = True
    use_static: bool = True
    lint_profile: str = ""            # ansible-lint --profile (e.g. "production")
    lint_offline: bool = True         # don't let ansible-lint install requirements.yml
    fail_on: str = "high"             # exit non-zero at or above this severity
    min_severity: str = "info"        # hide findings below this in reports
    min_confidence: float = 0.5       # drop LLM findings below this confidence
    exclude: list[str] = field(default_factory=lambda: [
        ".git/**", ".tox/**", ".venv/**", "venv/**", "**/node_modules/**",
        ".cache/**", "collections/ansible_collections/**",
        "**/.github/**", "**/molecule/**/.cache/**",
    ])
    house_rules: str = ""             # extra instructions appended to the LLM prompt
    disabled_rules: list[str] = field(default_factory=list)  # e.g. ["AR004", "no-changed-when"]

    @property
    def fail_on_severity(self) -> Severity:
        return Severity.parse(self.fail_on, Severity.HIGH)

    @property
    def min_severity_level(self) -> Severity:
        return Severity.parse(self.min_severity, Severity.INFO)

    def update(self, data: dict, *, trusted: bool = True, source: str = "") -> list[str]:
        """Apply a dict of settings. Returns warnings."""
        warnings = []
        known = {f.name: f for f in fields(self)}
        for key, value in (data or {}).items():
            key = key.replace("-", "_")
            if key not in known:
                warnings.append(f"{source}: unknown config key '{key}' ignored")
                continue
            if not trusted and key in REPO_UNTRUSTED_KEYS:
                warnings.append(
                    f"{source}: '{key}' ignored in a repo config "
                    "(set ANSREVIEW_TRUST_REPO_CONFIG=1 to allow)")
                continue
            if value is None:
                continue
            setattr(self, key, value)
        return warnings


ENV_MAP = {
    "OLLAMA_HOST": "ollama_host",
    "ANSREVIEW_OLLAMA_HOST": "ollama_host",
    "ANSREVIEW_MODEL": "model",
    "ANSREVIEW_API_KEY": "api_key",
    "ANSREVIEW_LANGUAGE": "language",
    "ANSREVIEW_WORKERS": "workers",
    "ANSREVIEW_NUM_CTX": "num_ctx",
    "ANSREVIEW_TIMEOUT": "timeout",
    "ANSREVIEW_FAIL_ON": "fail_on",
}


def _read_yaml(path: Path) -> dict:
    try:
        with path.open() as fh:
            data = yaml.safe_load(fh) or {}
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def _normalise_host(host: str) -> str:
    host = host.strip().rstrip("/")
    if host and "://" not in host:
        host = "http://" + host
    # OLLAMA_HOST is often "0.0.0.0:11434" on the server itself
    return host.replace("://0.0.0.0", "://127.0.0.1")


def load_config(root: Path, explicit: Path | None = None) -> tuple[Config, list[str]]:
    cfg = Config()
    warnings: list[str] = []

    for name in CONFIG_NAMES:
        p = Path.home() / name
        if p.is_file():
            warnings += cfg.update(_read_yaml(p), source=str(p))
            break

    trust_repo = os.environ.get("ANSREVIEW_TRUST_REPO_CONFIG") == "1"
    for name in CONFIG_NAMES:
        p = root / name
        if p.is_file():
            warnings += cfg.update(_read_yaml(p), trusted=trust_repo, source=str(p))
            break

    if explicit:
        warnings += cfg.update(_read_yaml(explicit), source=str(explicit))

    int_keys = {"workers", "num_ctx", "timeout"}
    for env, key in ENV_MAP.items():
        val = os.environ.get(env)
        if val:
            setattr(cfg, key, int(val) if key in int_keys else val)

    cfg.ollama_host = _normalise_host(cfg.ollama_host)
    return cfg, warnings
