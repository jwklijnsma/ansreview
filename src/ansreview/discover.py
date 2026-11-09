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
# A folder is a role when one of these has a main.yml, wherever the folder sits: Ansible
# also finds roles next to the playbook, not only under roles/.
ROLE_MARKER_DIRS = ("tasks", "handlers", "defaults", "vars", "meta")
MAIN_FILES = ("main.yml", "main.yaml")
PROJECT_MARKERS = ("ansible.cfg", "roles", "playbooks", "site.yml", "site.yaml", "group_vars",
                   "host_vars", "inventory", "inventories", "requirements.yml", "hosts.yml",
                   "hosts.yaml", "hosts.ini", "inventory.yml", "inventory.ini", "galaxy.yml")
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


def _role_root(rel_parts: tuple[str, ...], role_roots: set[str] = frozenset()) -> str | None:
    """Return the role folder a path lives in ('roles/<name>', '<name>', '' for a
    single-role repository), or None."""
    for i, part in enumerate(rel_parts[:-1]):
        if part == "roles" and i + 2 < len(rel_parts) and rel_parts[i + 2] in ROLE_DIRS:
            return "/".join(rel_parts[: i + 2])
    # Deepest known role folder whose next path part is a role subfolder (tasks/, ...).
    for depth in range(len(rel_parts) - 2, -1, -1):
        root = "/".join(rel_parts[:depth])
        if root in role_roots and rel_parts[depth] in ROLE_DIRS:
            return root
    return None


def _role_roots(rels: list[str]) -> set[str]:
    """Folders that are roles because they hold tasks/main.yml, defaults/main.yml, ..."""
    roots = set()
    for rel in rels:
        parts = rel.split("/")
        if len(parts) >= 2 and parts[-1] in MAIN_FILES and parts[-2] in ROLE_MARKER_DIRS:
            root = "/".join(parts[:-2])
            # not inside another role's files/ or templates/
            if not any(seg in ("files", "templates") for seg in parts[:-2]):
                roots.add(root)
    return roots


def is_role_dir(d: Path) -> bool:
    return any((d / sub / name).is_file() for sub in ROLE_MARKER_DIRS for name in MAIN_FILES)


def looks_like_project(p: Path, max_yaml: int = 25) -> bool:
    """Is this folder an Ansible project? Known marker files, a single role, roles next to
    the playbooks (any subfolder with tasks/main.yml, ...), or playbooks at the top."""
    try:
        if any((p / m).exists() for m in PROJECT_MARKERS) or is_role_dir(p):
            return True
        entries = sorted(p.iterdir())[:500]
    except OSError:
        return False
    for e in entries:
        if e.is_dir() and not e.name.startswith(".") and is_role_dir(e):
            return True
    ymls = [e for e in entries if e.suffix.lower() in YAML_EXT and e.is_file()
            and not e.is_symlink() and e.stat().st_size <= 256 * 1024]
    return any(_looks_like_playbook(e) for e in ymls[:max_yaml])


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

    files = list(iter_files(root, exclude))
    role_roots = _role_roots([rel for _, rel in files])
    for path, rel in files:
        parts = tuple(rel.split("/"))
        role = _role_root(parts, role_roots)
        if role is not None:
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

    def role_name(r: str) -> str:
        if r == "":
            return root.resolve().name or "role"     # single-role repository
        if r.count("/") == 1 and r.startswith("roles/"):
            return r.split("/")[-1]
        return r

    units = [Unit("role", role_name(r), sorted(f)) for r, f in sorted(roles.items())]
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
