"""ansreview command line."""

from __future__ import annotations

import argparse
from fnmatch import fnmatch
from pathlib import Path
import sys
import time

from . import __version__, lint
from .config import Config, load_config
from .discover import discover, filter_units, git_changed_files, git_changed_lines
from .models import ScanResult, Severity
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
        client = OllamaClient(cfg.ollama_host, cfg.model, api_key=cfg.api_key,
                              timeout=int(cfg.timeout), retries=int(cfg.retries),
                              temperature=float(cfg.temperature), num_ctx=int(cfg.num_ctx))
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
    if not argv or (argv[0] not in {"scan", "doctor", "rules", "init", "-h", "--help", "--version"}):
        argv.insert(0, "scan")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
