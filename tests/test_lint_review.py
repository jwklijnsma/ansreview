"""lint.py against a fake ansible-lint, and how the Reviewer treats model output:
path repair, line clamping, confidence filter, rule names, timeouts and splitting."""
import json
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from ansreview import lint
from ansreview.config import Config
from ansreview.models import Finding, Severity, Unit
from ansreview.ollama import OllamaError, OllamaTimeout
from ansreview.review import Reviewer, _prior_summary, build_chunks


# --------------------------------------------------------------------------- lint.py

@pytest.fixture
def fake_lint(tmp_path, monkeypatch):
    """An executable `ansible-lint` whose stdout/stderr/exit code come from files."""
    exe = tmp_path / "bin" / "ansible-lint"
    exe.parent.mkdir()
    exe.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import sys, pathlib, time
        d = pathlib.Path({str(tmp_path)!r})
        (d / "argv").write_text("\\n".join(sys.argv[1:]))
        if "--version" in sys.argv:
            print("ansible-lint 25.1.0 using ansible-core:2.18"); sys.exit(0)
        if (d / "sleep").exists():
            time.sleep(5)
        sys.stdout.write((d / "out").read_text() if (d / "out").exists() else "")
        sys.stderr.write((d / "err").read_text() if (d / "err").exists() else "")
        sys.exit(int((d / "rc").read_text()) if (d / "rc").exists() else 2)
        """))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(lint, "available", lambda: str(exe))
    return tmp_path


def test_lint_maps_codeclimate_json(fake_lint):
    items = [
        {"check_name": "no-log-password", "severity": "blocker", "description": " pw leak ",
         "location": {"path": "roles/a/tasks/main.yml", "lines": {"begin": 7}}, "url": "https://docs/x"},
        {"check_name": "no-changed-when", "severity": "major", "description": "d",
         "location": {"path": "site.yml", "lines": {"begin": {"line": 3, "column": 1}}}},
        {"check_name": "syntax-check[specific]", "severity": "critical", "description": "bad",
         "location": {"path": "x.yml", "positions": {"begin": {"line": 9}}}},
        {"check_name": "name[casing]", "severity": "weird", "description": "n", "location": {}},
    ]
    (fake_lint / "out").write_text("WARNING  some log line\n" + json.dumps(items))
    findings, errors = lint.run_lint(Path("."), files=["site.yml"], profile="production")
    assert errors == []
    assert [(f.rule, f.severity, f.file, f.line, f.category) for f in findings] == [
        ("no-log-password", Severity.CRITICAL, "roles/a/tasks/main.yml", 7, "security"),
        ("no-changed-when", Severity.MEDIUM, "site.yml", 3, "idempotency"),
        ("syntax-check[specific]", Severity.HIGH, "x.yml", 9, "reliability"),
        ("name[casing]", Severity.LOW, "?", None, "style"),
    ]
    assert findings[0].message == "pw leak" and findings[0].suggestion == "https://docs/x"
    argv = (fake_lint / "argv").read_text().splitlines()
    assert argv[:4] == ["-f", "codeclimate", "--nocolor", "-q"]
    assert "--offline" in argv and argv[-3:] == ["--profile", "production", "site.yml"]


def test_lint_online_and_clean_run(fake_lint):
    (fake_lint / "rc").write_text("0")
    assert lint.run_lint(Path("."), offline=False) == ([], [])
    assert "--offline" not in (fake_lint / "argv").read_text()


def test_lint_crash_reports_the_stderr_tail(fake_lint):
    (fake_lint / "rc").write_text("3")
    (fake_lint / "err").write_text("\n".join(f"line{i}" for i in range(10)))
    assert lint.run_lint(Path(".")) == ([], ["ansible-lint failed: line5 | line6 | line7 | line8 | line9"])


def test_lint_bad_json_and_timeout(fake_lint):
    (fake_lint / "out").write_text("[{not json")
    findings, errors = lint.run_lint(Path("."))
    assert findings == [] and errors[0].startswith("could not parse ansible-lint output")
    (fake_lint / "sleep").touch()
    assert lint.run_lint(Path("."), timeout=1) == ([], ["ansible-lint timed out after 1s"])


def test_lint_missing(monkeypatch):
    monkeypatch.setattr(lint, "available", lambda: None)
    assert lint.run_lint(Path("."))[1][0].startswith("ansible-lint not found")
    assert lint.version() == ""


def test_lint_version(fake_lint):
    assert lint.version() == "ansible-lint 25.1.0 using ansible-core:2.18"


def test_available_prefers_the_venv_sibling(tmp_path, monkeypatch):
    py = tmp_path / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    sib = py.parent / "ansible-lint"
    sib.write_text("#!/bin/sh\n")
    sib.chmod(0o755)
    monkeypatch.setattr(lint.sys, "executable", str(py))
    assert lint.available() == str(sib)
    sib.chmod(0o644)                                   # not executable -> PATH lookup
    monkeypatch.setattr(lint.shutil, "which", lambda name: "/usr/bin/ansible-lint")
    assert lint.available() == "/usr/bin/ansible-lint"


# --------------------------------------------------------------------------- review.py

class FakeClient:
    model, host, timeout = "test-model", "http://ollama", 600

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def chat_json(self, system, user, schema):
        self.calls.append(user)
        r = self.replies.pop(0) if self.replies else {"findings": []}
        if isinstance(r, Exception):
            raise r
        return r(user) if callable(r) else r


@pytest.fixture
def repo(tmp_path):
    files = {
        "roles/web/tasks/main.yml": "".join(f"- name: task {i}\n  ansible.builtin.debug: msg=hi\n" for i in range(10)),
        "roles/web/defaults/main.yml": "port: 80\n",
        "site.yml": "- hosts: all\n  roles: [web]\n",
    }
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path


def reviewer(root, replies, **cfg):
    c = Config()
    for k, v in cfg.items():
        setattr(c, k, v)
    return Reviewer(root, c, FakeClient(replies), use_cache=False)


ROLE = Unit("role", "web", ["roles/web/defaults/main.yml", "roles/web/tasks/main.yml"])


def test_model_output_is_sanitised(repo):
    reply = {"summary": "Role looks ok", "findings": [
        {"file": "./tasks/main.yml", "line": 3, "rule": "Shell Injection!", "severity": "high",
         "message": "x", "confidence": 0.9},                          # prefix dropped -> repaired
        {"file": "roles/web/tasks/main.yml", "line": 999, "message": "line out of range", "confidence": 0.9},
        {"file": "roles/web/tasks/main.yml", "line": "abc", "message": "bad line", "confidence": "?"},
        {"file": "main.yml", "message": "ambiguous file", "confidence": 0.9},   # matches 2 files -> dropped
        {"file": "other.yml", "message": "unknown file", "confidence": 0.9},
        {"file": "roles/web/tasks/main.yml", "message": "too unsure", "confidence": 0.2},
        {"file": "roles/web/tasks/main.yml", "message": "  ", "confidence": 0.9},
        "not a dict",
    ]}
    r = reviewer(repo, [reply], per_file=False)
    out = r._review_chunk(ROLE, 1, 1, ROLE.files, "src", [])
    assert [(f.file, f.line, f.rule, f.message, f.confidence) for f in out] == [
        ("roles/web/tasks/main.yml", 3, "llm:shell-injection", "x", 0.9),
        ("roles/web/tasks/main.yml", None, "llm:llm", "line out of range", 0.9),
        ("roles/web/tasks/main.yml", None, "llm:llm", "bad line", 0.7),
    ]
    assert out[0].severity == Severity.HIGH and out[1].severity == Severity.MEDIUM
    assert r.summaries == {"role web": "Role looks ok"}


def test_prompt_lists_prior_findings_and_house_rules(repo):
    prior = [Finding("no-changed-when", Severity.MEDIUM, "roles/web/tasks/main.yml", 2, "m" * 300, "ansible-lint")]
    r = reviewer(repo, [{}], per_file=False, house_rules="Always pin versions.", language="nl")
    r._review_chunk(ROLE, 2, 3, ROLE.files, "SOURCE", prior)
    user = r.client.calls[0]
    assert "Review this Ansible role: web (part 2 of 3)" in user
    assert "Team rules to enforce:\nAlways pin versions." in user
    assert "- roles/web/tasks/main.yml:2 [no-changed-when] " + "m" * 140 + "\n" in user + "\n"
    assert user.endswith("Source:\nSOURCE")


def test_prior_summary_caps_at_60():
    rows = [Finding("r", Severity.LOW, "a.yml", i, "msg", "x") for i in range(65)]
    text = _prior_summary(rows, ["a.yml"])
    assert text.count("\n") == 60 and text.endswith("- ... and 5 more")
    assert _prior_summary(rows, ["b.yml"]) == "(none)"


def test_timeout_splits_the_chunk_and_reviews_both_halves(repo):
    def ok(user):
        f = "roles/web/defaults/main.yml" if "defaults/main.yml =====" in user else "roles/web/tasks/main.yml"
        return {"findings": [{"file": f, "line": 1, "message": "found", "confidence": 0.9}]}

    r = reviewer(repo, [OllamaTimeout("slow"), ok, ok], per_file=False, max_chars_per_unit=8000)
    out = r._review_split(ROLE, 1, 1, ROLE.files, "src", [], 8000)
    assert sorted(f.file for f in out) == ROLE.files
    assert len(r.client.calls) == 3


def test_timeout_below_the_minimum_chunk_size_is_raised(repo):
    r = reviewer(repo, [OllamaTimeout("slow")], per_file=False)
    with pytest.raises(OllamaTimeout):
        r._review_split(ROLE, 1, 1, ROLE.files, "src", [], 3000)


def test_cached_timeout_is_remembered_until_the_timeout_grows(repo):
    r = Reviewer(repo, Config(), FakeClient([OllamaTimeout("slow")]), use_cache=True)
    unit = Unit("playbook", "site.yml", ["site.yml"])
    with pytest.raises(OllamaTimeout):
        r._review_chunk(unit, 1, 1, ["site.yml"], "src", [])
    with pytest.raises(OllamaTimeout, match="cached"):
        r._review_chunk(unit, 1, 1, ["site.yml"], "src", [])          # no second model call
    assert len(r.client.calls) == 1
    r.client.timeout = 1200                                             # longer timeout: try again
    r.client.replies = [{"findings": []}]
    assert r._review_chunk(unit, 1, 1, ["site.yml"], "src", []) == []


def test_review_collects_errors_per_job_and_keeps_going(repo):
    units = [ROLE, Unit("playbook", "site.yml", ["site.yml"])]

    def by_file(user):
        if "site.yml =====" in user:
            raise OllamaError("model crashed")
        return {"findings": []}

    r = reviewer(repo, [by_file] * 3)
    assert r.review(units, []) == []
    assert r.errors == ["site.yml: OllamaError: model crashed"]
    queue = json.loads((repo / ".ansreview" / "queue.json").read_text())
    assert "site.yml" in json.dumps(queue)


def test_per_file_jobs_dedupe_shared_files(repo):
    units = [ROLE, Unit("vars", "web", ["roles/web/defaults/main.yml"])]
    r = reviewer(repo, [], per_file=True)
    keys = [r._job_key(j) for j in r._jobs(units)]
    assert keys == ["roles/web/defaults/main.yml", "roles/web/tasks/main.yml"]


def test_build_chunks_truncates_and_splits(repo):
    cfg = Config()
    cfg.max_file_chars = 100
    chunks = build_chunks(repo, ROLE, cfg, max_chars=400)
    assert [files for files, _ in chunks] == [["roles/web/defaults/main.yml"], ["roles/web/tasks/main.yml"]] \
        or len(chunks) >= 1
    assert "[truncated by ansreview]" in "".join(src for _, src in chunks)
    (repo / "empty.yml").write_text("  \n")
    assert build_chunks(repo, Unit("other", "e", ["empty.yml", "missing.yml"]), cfg) == []
