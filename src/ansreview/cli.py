"""ansreview command line."""

from __future__ import annotations

import argparse
from fnmatch import fnmatch
import json
from pathlib import Path
import subprocess
import sys
import time

from . import __version__, lint
from .config import Config, load_config
from .discover import discover, filter_units, git_changed_files, git_changed_lines
from .fix import Fixer, fix_report_md
from .models import Finding, ScanResult, Severity
from .ollama import OllamaClient
from .report import terminal_summary, write_reports
from .review import Reviewer
from .static import RULE_DOCS, run_static

EXIT_OK, EXIT_FINDINGS, EXIT_ERROR = 0, 1, 2

SAMPLE_CONFIG = """\
# ansreview configuration. Put it in the repo root or in ~/.ansreview.yml.
# ollama_host and api_key are only honoured from ~/.ansreview.yml, env or CLI
# (a repo cannot redirect your review to another server).

model: qwen2.5-coder:14b        # any Ollama model; qwen3-coder / qwen2.5-coder:32b if VRAM allows
language: en                    # en | nl
num_ctx: 16384                  # context window sent to Ollama
workers: 1                      # parallel requests; raise if OLLAMA_NUM_PARALLEL > 1
fail_on: high                   # exit 1 when a finding is at/above: info|low|medium|high|critical
min_severity: info              # hide findings below this level in reports
min_confidence: 0.5             # drop LLM findings it is unsure about
lint_profile: ""                # ansible-lint profile, e.g. production
lint_offline: true              # set false to let ansible-lint install requirements.yml collections
disabled_rules: []              # e.g. [AR009, yaml[line-length]]
exclude:
  - ".git/**"
  - "collections/ansible_collections/**"
  - "**/molecule/**/.cache/**"

house_rules: |
  - Variables in a role must be prefixed with the role name.
  - Use fully qualified collection names (ansible.builtin.*).
  - Secrets must come from ansible-vault or HashiCorp Vault, never plain vars.
"""


def _progress(quiet: bool):
    def emit(msg: str):
        if not quiet:
            print(msg, file=sys.stderr, flush=True)
    return emit


def make_client(cfg: Config, temperature: float | None = None) -> OllamaClient:
    return OllamaClient(cfg.ollama_host, cfg.model, api_key=cfg.api_key,
                        timeout=int(cfg.timeout), retries=int(cfg.retries),
                        temperature=float(cfg.temperature if temperature is None else temperature),
                        num_ctx=int(cfg.num_ctx), num_predict=int(cfg.num_predict),
                        deadline=int(cfg.deadline), keep_alive=str(cfg.keep_alive))


def _apply_cli(cfg: Config, a: argparse.Namespace):
    for attr, key in [("model", "model"), ("host", "ollama_host"), ("lang", "language"),
                      ("workers", "workers"), ("num_ctx", "num_ctx"), ("fail_on", "fail_on"),
                      ("min_severity", "min_severity"), ("timeout", "timeout"),
                      ("lint_profile", "lint_profile"), ("min_confidence", "min_confidence")]:
        val = getattr(a, attr, None)
        if val is not None:
            setattr(cfg, key, val)
    if getattr(a, "no_llm", False):
        cfg.use_llm = False
    if getattr(a, "no_lint", False):
        cfg.use_lint = False
    if getattr(a, "no_static", False):
        cfg.use_static = False
    for pat in getattr(a, "exclude", None) or []:
        cfg.exclude.append(pat)
    for rule in getattr(a, "disable", None) or []:
        cfg.disabled_rules.append(rule)
    if "://" not in cfg.ollama_host:
        cfg.ollama_host = "http://" + cfg.ollama_host
    cfg.ollama_host = cfg.ollama_host.rstrip("/")


def touches_change(f, changed_lines: dict) -> bool:
    """True if the finding's task/line range overlaps an added or modified line."""
    lines = changed_lines.get(f.file, set())
    if lines is None or f.line is None:      # new file, or file-level finding
        return True
    end = f.end_line if f.end_line and f.end_line >= f.line else f.line + 2
    return any(f.line <= n <= end for n in lines)


def cmd_scan(a: argparse.Namespace) -> int:
    root = Path(a.path).resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return EXIT_ERROR
    cfg, warnings = load_config(root, Path(a.config) if a.config else None)
    _apply_cli(cfg, a)
    say = _progress(a.quiet)
    for w in warnings:
        say(f"warning: {w}")
    t0 = time.time()

    units = discover(root, cfg.exclude)
    changed: set[str] | None = None
    changed_lines: dict[str, set[int] | None] = {}
    if a.diff:
        try:
            changed = git_changed_files(root, a.diff)
            if a.diff_scope == "lines":
                changed_lines = git_changed_lines(root, a.diff)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_ERROR
        units = filter_units(units, changed)
        say(f"Diff mode vs {a.diff}: {len(changed)} changed file(s), {len(units)} unit(s) affected")
    if a.only:
        units = [u for u in units if any(
            fnmatch(u.name, p) or any(fnmatch(f, p) or f.startswith(p.rstrip("/") + "/")
                                      for f in u.files) for p in a.only)]
    say(f"ansreview {__version__}: {root.name} — {len(units)} unit(s): "
        + ", ".join(f"{k} {sum(1 for u in units if u.kind == k)}"
                    for k in ("role", "playbook", "vars", "other")))
    if not units:
        say("Nothing to review.")

    files = sorted({f for u in units for f in u.files})
    scope = sorted(changed & set(files)) if changed is not None else files
    res = ScanResult(root=str(root), units=units, findings=[])

    if cfg.use_static and scope:
        say(f"Static checks on {len(scope)} file(s)…")
        found, errs = run_static(root, scope, cfg.disabled_rules)
        res.findings += found
        res.errors += errs

    if cfg.use_lint and units:
        lint_targets = None
        if changed is not None or a.only:
            lint_targets = [f for f in scope if f.endswith((".yml", ".yaml"))]
        if lint_targets != []:
            say("Running ansible-lint…")
            found, errs = lint.run_lint(root, lint_targets, cfg.lint_profile, offline=bool(cfg.lint_offline))
            allowed = set(files)
            res.findings += [f for f in found if f.file in allowed
                             and f.rule not in cfg.disabled_rules
                             and f.rule.split("[")[0] not in cfg.disabled_rules]
            res.errors += errs

    summaries: dict[str, str] = {}
    if cfg.use_llm and units:
        client = make_client(cfg)
        try:
            client.version()
            reachable = True
        except Exception as exc:
            reachable = False
            msg = f"Ollama not reachable at {cfg.ollama_host} ({exc.__class__.__name__}); LLM review skipped"
            res.errors.append(msg)
            say("warning: " + msg)
            if a.require_llm:
                print("error: --require-llm set and Ollama is unreachable", file=sys.stderr)
                return EXIT_ERROR
        if reachable:
            res.model = cfg.model
            reviewer = Reviewer(root, cfg, client, say)
            res.findings += reviewer.review(units, list(res.findings))
            res.errors += reviewer.errors
            summaries = reviewer.summaries
            res.stats["tokens_in"] = client.tokens_in
            res.stats["tokens_out"] = client.tokens_out

    # In diff mode only report on changed files/lines (the LLM still saw whole units).
    if changed is not None:
        res.findings = [f for f in res.findings if f.file in changed]
        if a.diff_scope == "lines":
            res.findings = [f for f in res.findings if touches_change(f, changed_lines)]
    floor = cfg.min_severity_level
    res.findings = [f for f in res.findings if f.severity >= floor
                    and f.rule not in cfg.disabled_rules]
    res.stats["seconds"] = round(time.time() - t0, 1)

    formats = [x for x in (a.format or "md,json").split(",") if x]
    out_dir = Path(a.output) if a.output else root / ".ansreview"
    written = write_reports(res, out_dir, formats, cfg.language, summaries)
    if not a.quiet:
        print(terminal_summary(res), file=sys.stderr)
        for p in written:
            print(f"Report: {p}", file=sys.stderr)
        print(f"Done in {res.stats['seconds']}s", file=sys.stderr)

    worst = res.worst()
    if worst is not None and worst >= cfg.fail_on_severity:
        return EXIT_FINDINGS
    return EXIT_OK


def cmd_doctor(a: argparse.Namespace) -> int:
    root = Path(a.path).resolve()
    cfg, warnings = load_config(root, Path(a.config) if a.config else None)
    _apply_cli(cfg, a)
    ok = True
    for w in warnings:
        print(f"! {w}")
    v = lint.version()
    print(f"{'✓' if v else '✗'} ansible-lint: {v or 'not installed (pip install ansible-lint)'}")
    client = OllamaClient(cfg.ollama_host, cfg.model, api_key=cfg.api_key)
    try:
        print(f"✓ Ollama {client.version()} at {cfg.ollama_host}")
        if client.has_model():
            print(f"✓ model {cfg.model} is available")
        else:
            ok = False
            names = ", ".join(client.models()[:10]) or "none"
            print(f"✗ model {cfg.model} not pulled (have: {names}) — run: ollama pull {cfg.model}")
    except Exception as exc:
        ok = False
        print(f"✗ Ollama not reachable at {cfg.ollama_host}: {exc}")
    units = discover(root, cfg.exclude)
    print(f"✓ {root}: {len(units)} reviewable unit(s)")
    return EXIT_OK if ok else EXIT_ERROR


def cmd_rules(_: argparse.Namespace) -> int:
    for rule, doc in RULE_DOCS.items():
        print(f"{rule}  {doc}")
    print("\nPlus all ansible-lint rules, and llm:* findings from the model.")
    return EXIT_OK


def cmd_init(a: argparse.Namespace) -> int:
    target = Path(a.path).resolve() / ".ansreview.yml"
    if target.exists() and not a.force:
        print(f"{target} already exists (use --force to overwrite)", file=sys.stderr)
        return EXIT_ERROR
    target.write_text(SAMPLE_CONFIG)
    print(f"Wrote {target}")
    return EXIT_OK


def _load_report(path: Path) -> tuple[list[Finding], str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for d in data.get("findings", []):
        try:
            out.append(Finding(
                rule=d["rule"], severity=Severity.parse(d.get("severity")), file=d["file"],
                line=d.get("line"), message=d.get("message", ""), source=d.get("source", ""),
                category=d.get("category", "general"), suggestion=d.get("suggestion", ""),
                confidence=d.get("confidence"), end_line=d.get("end_line")))
        except KeyError:
            continue
    return out, str(data.get("root", ""))


def _git_dirty(root: Path) -> list[str] | None:
    """Uncommitted paths (ignoring .ansreview/), or None when root is not a git repo."""
    try:
        proc = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", ".",
                               ":(exclude).ansreview"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return [l[3:] for l in proc.stdout.splitlines() if l.strip()]


def cmd_fix(a: argparse.Namespace) -> int:
    root = Path(a.path).resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return EXIT_ERROR
    cfg, warnings = load_config(root, Path(a.config) if a.config else None)
    _apply_cli(cfg, a)
    for attr in ("fix_min_severity", "fix_min_confidence", "lint_fix_rules", "fix_batch_size"):
        if getattr(a, attr, None) is not None:
            setattr(cfg, attr, getattr(a, attr))
    say = _progress(a.quiet)
    for w in warnings:
        say(f"warning: {w}")

    dirty = _git_dirty(root)
    if dirty is None and not a.dry_run:
        say("warning: not a git repository; a backup of every changed file goes to .ansreview/backup/")
    elif dirty and not (a.dry_run or a.allow_dirty):
        print("error: the repository has uncommitted changes:\n  " + "\n  ".join(dirty[:10])
              + ("\n  …" if len(dirty) > 10 else "")
              + "\nCommit or stash them first so `git diff` shows only ansreview's fixes, "
                "or pass --allow-dirty (or --dry-run).", file=sys.stderr)
        return EXIT_ERROR

    out_dir = Path(a.output) if a.output else root / ".ansreview"
    findings: list[Finding] = []
    if not a.no_llm:
        report = Path(a.from_report) if a.from_report else out_dir / "report.json"
        if not report.is_file():
            say(f"No report at {report}; running a scan first…")
            argv = ["scan", str(root), "-q", "-f", "json", "-o", str(report.parent)]
            for flag, val in (("--host", a.host), ("--model", a.model), ("-c", a.config),
                              ("--lang", a.lang)):
                if val:
                    argv += [flag, val]
            for p in a.only or []:
                argv += ["--only", p]
            cmd_scan(build_parser().parse_args(argv))
        if report.is_file():
            findings, report_root = _load_report(report)
            if report_root and Path(report_root).name != root.name:
                say(f"warning: {report} was made for '{Path(report_root).name}', not '{root.name}'")
            say(f"Using {report} ({sum(1 for f in findings if f.source == 'llm')} model findings)")

    client = None
    if not a.no_llm:
        client = make_client(cfg, temperature=0.0)
        try:
            client.version()
        except Exception as exc:
            say(f"warning: Ollama not reachable at {cfg.ollama_host} ({exc.__class__.__name__}); "
                "model fixes skipped")
            client = None

    fixer = Fixer(root, cfg, client=client, vault_password_file=a.vault_password_file,
                  lint_fix=not a.no_lint_fix, static_fix=not a.no_static_fix,
                  only=a.only, say=say, use_cache=not a.no_cache)
    run = fixer.run(findings)

    out_dir.mkdir(parents=True, exist_ok=True)
    patch = out_dir / "fix.patch"
    patch.write_text(run.diff(), encoding="utf-8")
    written = []
    if not a.dry_run:
        backup = out_dir / "backup" / time.strftime("%Y%m%d-%H%M%S") if dirty is None else None
        for rel, (old, new) in sorted(run.changed.items()):
            target = root / rel
            try:
                current = target.read_text(encoding="utf-8", errors="surrogateescape")
            except OSError as exc:
                run.errors.append(f"{rel}: {exc}")
                continue
            if current != old:
                run.errors.append(f"{rel}: changed on disk during the fix run; left alone")
                continue
            if backup:
                (backup / rel).parent.mkdir(parents=True, exist_ok=True)
                (backup / rel).write_text(old, encoding="utf-8", errors="surrogateescape")
            target.write_text(new, encoding="utf-8", errors="surrogateescape")
            written.append(rel)
    report_md = out_dir / "fix-report.md"
    report_md.write_text(fix_report_md(run, str(root), a.dry_run, cfg.language), encoding="utf-8")

    if not a.quiet:
        print("", file=sys.stderr)
        for stage in ("llm", "static", "lint"):
            print(f"  {stage:<6} fixed {run.count('fixed', stage):>3}   declined "
                  f"{run.count('declined', stage):>3}   failed {run.count('failed', stage):>3}   "
                  f"manual {run.count('manual', stage):>3}", file=sys.stderr)
        for e in run.errors[:10]:
            print(f"  ! {e}", file=sys.stderr)
        verb = "would change" if a.dry_run else "changed"
        print(f"\n{len(run.changed)} file(s) {verb}. Patch: {patch}\nReport: {report_md}",
              file=sys.stderr)
        if written and dirty is not None:
            print("Review with `git diff`; undo everything with `git checkout -- .`",
                  file=sys.stderr)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ansreview",
        description="Review an Ansible codebase with ansible-lint, static security checks "
                    "and a local Ollama model.")
    p.add_argument("--version", action="version", version=f"ansreview {__version__}")
    sub = p.add_subparsers(dest="command")

    def common(sp):
        sp.add_argument("path", nargs="?", default=".", help="repository root (default: .)")
        sp.add_argument("-c", "--config", help="extra config file")
        sp.add_argument("-m", "--model", help="Ollama model (default qwen2.5-coder:14b)")
        sp.add_argument("--host", help="Ollama URL, e.g. http://gpu-box:11434")

    s = sub.add_parser("scan", help="review the codebase (default command)")
    common(s)
    s.add_argument("--diff", metavar="BASE", help="only review units changed vs a git ref, e.g. origin/main")
    s.add_argument("--diff-scope", choices=["lines", "files"], default="lines",
                   help="with --diff: report only on changed lines (default) or whole changed files")
    s.add_argument("--only", action="append", metavar="PATTERN",
                   help="limit to units/paths matching (repeatable), e.g. roles/nginx")
    s.add_argument("-f", "--format", help="md,json,sarif,gitlab (default md,json)")
    s.add_argument("-o", "--output", help="report directory (default <repo>/.ansreview)")
    s.add_argument("--lang", choices=["en", "nl"], help="report language")
    s.add_argument("--fail-on", choices=[x.label for x in Severity])
    s.add_argument("--min-severity", choices=[x.label for x in Severity])
    s.add_argument("--min-confidence", type=float)
    s.add_argument("--workers", type=int)
    s.add_argument("--num-ctx", type=int)
    s.add_argument("--timeout", type=int)
    s.add_argument("--lint-profile")
    s.add_argument("--exclude", action="append", metavar="GLOB")
    s.add_argument("--disable", action="append", metavar="RULE")
    s.add_argument("--no-llm", action="store_true", help="skip the Ollama review")
    s.add_argument("--no-lint", action="store_true", help="skip ansible-lint")
    s.add_argument("--no-static", action="store_true", help="skip built-in AR checks")
    s.add_argument("--require-llm", action="store_true", help="fail if Ollama is unreachable")
    s.add_argument("-q", "--quiet", action="store_true")
    s.set_defaults(func=cmd_scan)

    fx = sub.add_parser("fix", help="automatically fix findings (model edits, AR fixes, ansible-lint --fix)")
    common(fx)
    fx.add_argument("--from-report", metavar="FILE",
                    help="report.json to fix from (default <repo>/.ansreview/report.json; "
                         "a scan runs first when it does not exist)")
    fx.add_argument("--dry-run", action="store_true", help="only write fix.patch, change nothing")
    fx.add_argument("--allow-dirty", action="store_true",
                    help="run even with uncommitted changes in the repo")
    fx.add_argument("--only", action="append", metavar="PATH", help="limit to paths (repeatable)")
    fx.add_argument("--vault-password-file", metavar="FILE",
                    help="encrypt hard-coded secrets (AR001) in place with ansible-vault")
    fx.add_argument("--min-severity", dest="fix_min_severity",
                    choices=[x.label for x in Severity],
                    help="model fixes only for findings at/above this (default medium)")
    fx.add_argument("--min-confidence", dest="fix_min_confidence", type=float,
                    help="model fixes only at/above this confidence (default 0.8)")
    fx.add_argument("--lint-fix-rules", help="ansible-lint --fix write list (default all)")
    fx.add_argument("--no-llm", action="store_true", help="skip model fixes")
    fx.add_argument("--no-static-fix", action="store_true", help="skip built-in AR fixes")
    fx.add_argument("--no-lint-fix", action="store_true", help="skip ansible-lint --fix")
    fx.add_argument("--lang", choices=["en", "nl"])
    fx.add_argument("--workers", type=int,
                    help="parallel model requests; match OLLAMA_NUM_PARALLEL on the server")
    fx.add_argument("--timeout", type=int, help="max seconds without model output (default 600)")
    fx.add_argument("--batch-size", dest="fix_batch_size", type=int,
                    help="findings per model request (default 4; split further on timeout)")
    fx.add_argument("--no-cache", action="store_true",
                    help="ignore .ansreview/fix-cache.json and ask the model again")
    fx.add_argument("-o", "--output", help="where reports live (default <repo>/.ansreview)")
    fx.add_argument("-q", "--quiet", action="store_true")
    fx.set_defaults(func=cmd_fix)

    d = sub.add_parser("doctor", help="check ansible-lint, Ollama and the model")
    common(d)
    d.set_defaults(func=cmd_doctor)

    r = sub.add_parser("rules", help="list built-in rules")
    r.set_defaults(func=cmd_rules)

    i = sub.add_parser("init", help="write a sample .ansreview.yml")
    i.add_argument("path", nargs="?", default=".")
    i.add_argument("--force", action="store_true")
    i.set_defaults(func=cmd_init)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0] not in {"scan", "fix", "doctor", "rules", "init", "-h", "--help",
                                    "--version"}):
        argv.insert(0, "scan")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
