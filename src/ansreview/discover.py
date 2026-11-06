"""Find the reviewable units (roles, playbooks, vars) in an Ansible repository."""

from __future__ import annotations

from collections import defaultdict
from fnmatch import fnmatch
from pathlib import Path
import re
import subprocess

import yaml

from .models import Unit

YAML_EXT = {".yml", ".yaml"}
TEXT_EXT = YAML_EXT | {".j2", ".jinja", ".jinja2", ".cfg", ".ini", ".conf", ".sh", ".py", ".json", ""}
ROLE_DIRS = {"tasks", "handlers", "defaults", "vars", "meta", "templates", "files", "library", "filter_plugins"}
VARS_DIRS = {"group_vars", "host_vars", "inventory", "inventories"}
MAX_FILE_BYTES = 512 * 1024


def is_excluded(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch(rel, p) or fnmatch(rel + "/", p) for p in patterns)


def iter_files(root: Path, exclude: list[str]):
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if is_excluded(rel, exclude):
            continue
        if path.suffix.lower() not in TEXT_EXT:
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        yield path, rel


def _role_root(rel_parts: tuple[str, ...]) -> str | None:
    """Return 'roles/<name>' (or nested equivalent) if the path lives inside a role."""
    for i, part in enumerate(rel_parts[:-1]):
        if part == "roles" and i + 2 < len(rel_parts) and rel_parts[i + 2] in ROLE_DIRS:
            return "/".join(rel_parts[: i + 2])
    return None


def _looks_like_playbook(path: Path) -> bool:
    try:
        data = yaml.safe_load(path.read_text(errors="replace"))
    except yaml.YAMLError:
        return False
    if not isinstance(data, list) or not data:
        return False
    return any(
        isinstance(p, dict) and (
            "hosts" in p or "import_playbook" in p or "ansible.builtin.import_playbook" in p)
        for p in data
    )


def discover(root: Path, exclude: list[str]) -> list[Unit]:
    roles: dict[str, list[str]] = defaultdict(list)
    vars_units: dict[str, list[str]] = defaultdict(list)
    other: dict[str, list[str]] = defaultdict(list)
    playbooks: list[Unit] = []

    for path, rel in iter_files(root, exclude):
        parts = tuple(rel.split("/"))
        role = _role_root(parts)
        if role:
            roles[role].append(rel)
            continue
        if any(p in VARS_DIRS for p in parts[:-1]) or parts[0] in VARS_DIRS:
            top = next(i for i, p in enumerate(parts) if p in VARS_DIRS)
            vars_units["/".join(parts[: top + 1])].append(rel)
            continue
        if path.suffix.lower() in YAML_EXT:
            name = parts[-1]
            if name in {"requirements.yml", "requirements.yaml", "galaxy.yml", ".ansible-lint",
                        ".yamllint", ".yamllint.yml", ".ansreview.yml", ".gitlab-ci.yml",
                        ".pre-commit-config.yaml", "docker-compose.yml"}:
                continue
            if _looks_like_playbook(path):
                playbooks.append(Unit("playbook", rel, [rel]))
            else:
                parent = "/".join(parts[:-1]) or "."
                other[parent].append(rel)

    units = [Unit("role", r.split("/")[-1] if r.count("/") == 1 else r, sorted(f))
             for r, f in sorted(roles.items())]
    units += playbooks
    units += [Unit("vars", k, sorted(v)) for k, v in sorted(vars_units.items())]
    units += [Unit("other", k, sorted(v)) for k, v in sorted(other.items())
              if any(Path(x).suffix in YAML_EXT for x in v)]
    return units


def git_changed_files(root: Path, base: str) -> set[str]:
    """Files changed versus `base` (committed + staged + unstaged + untracked)."""
    def run(*args: str) -> list[str]:
        try:
            out = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                                 text=True, check=True, timeout=60)
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"git {' '.join(args)} failed: {getattr(exc, 'stderr', exc)}") from exc
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]

    prefix = run("rev-parse", "--show-prefix")
    prefix = prefix[0] if prefix else ""
    files = set(run("diff", "--name-only", "--relative", f"{base}...HEAD"))
    files |= set(run("diff", "--name-only", "--relative", "HEAD"))
    files |= set(run("ls-files", "--others", "--exclude-standard"))
    return {f[len(prefix):] if prefix and f.startswith(prefix) else f for f in files}


HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def git_changed_lines(root: Path, base: str) -> dict[str, set[int] | None]:
    """Map file -> set of added/modified line numbers vs `base` (None = whole file is new)."""
    def diff(*args: str) -> str:
        try:
            return subprocess.run(["git", "-C", str(root), "diff", "-U0", "--relative",
                                   "--no-color", *args], capture_output=True, text=True,
                                  check=True, timeout=60).stdout
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"git diff failed: {getattr(exc, 'stderr', exc)}") from exc

    result: dict[str, set[int] | None] = {}
    # Diff the merge-base with the working tree: committed + staged + unstaged in one go.
    try:
        mb = subprocess.run(["git", "-C", str(root), "merge-base", base, "HEAD"],
                            capture_output=True, text=True, check=True, timeout=60).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"git merge-base {base} HEAD failed: {getattr(exc, 'stderr', exc)}") from exc
    current = None
    for line in diff(mb).splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            current = None if path == "/dev/null" else path[2:] if path.startswith("b/") else path
            if current is not None:
                result.setdefault(current, set())
        elif current is not None:
            m = HUNK_RE.match(line)
            if m:
                start, count = int(m.group(1)), int(m.group(2) or 1)
                lines = result[current]
                if lines is not None:
                    lines.update(range(start, start + count))
    for f in git_changed_files(root, base):
        if f not in result:
            result[f] = None   # untracked / new: every line counts
    return result


def filter_units(units: list[Unit], changed: set[str]) -> list[Unit]:
    """Keep units that contain at least one changed file (whole unit is still reviewed)."""
    return [u for u in units if any(f in changed for f in u.files)]
