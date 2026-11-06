import json
from pathlib import Path
import shutil
import subprocess

import pytest

from ansreview.cli import main
from ansreview.config import load_config
from ansreview.discover import discover
from ansreview.models import Severity
from ansreview.ollama import parse_json_lenient
from ansreview.static import run_static

import mock_ollama

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sample-repo"


@pytest.fixture()
def repo(tmp_path):
    dst = tmp_path / "repo"
    shutil.copytree(SAMPLE, dst)
    return dst


@pytest.fixture()
def ollama():
    srv, url = mock_ollama.start()
    yield url
    srv.shutdown()


def rules_of(findings):
    return {f.rule for f in findings}


def test_discover_units(repo):
    units = discover(repo, [])
    kinds = {(u.kind, u.name) for u in units}
    assert ("role", "webserver") in kinds
    assert ("playbook", "site.yml") in kinds
    assert ("playbook", "playbooks/bootstrap.yml") in kinds
    assert ("vars", "group_vars") in kinds


def test_static_rules_fire(repo):
    files = [u for unit in discover(repo, []) for u in unit.files]
    findings, errors = run_static(repo, files)
    assert not errors
    assert {"AR001", "AR002", "AR003", "AR005", "AR006", "AR007", "AR008", "AR009",
            "AR011", "AR012", "AR014", "AR015"} <= rules_of(findings)
    # vault-referenced token must NOT be flagged as hard-coded
    assert not any(f.rule == "AR001" and "api_token" in f.message for f in findings)
    # secret values are masked in messages
    assert not any("Sup3rS3cret" in f.message for f in findings)


def test_static_ignores_vault_and_placeholders(tmp_path):
    (tmp_path / "vars.yml").write_text(
        "db_password: !vault |\n  $ANSIBLE_VAULT;1.1;AES256\n  6162\n"
        "admin_password: \"{{ vault_admin_password }}\"\n"
        "password_min_length: 12\n"
        "secret_file: /etc/app/secret\n"
        "api_key: \"\"\n")
    findings, _ = run_static(tmp_path, ["vars.yml"])
    assert findings == []


def test_static_disable_rule(repo):
    findings, _ = run_static(repo, ["playbooks/bootstrap.yml"], disabled=["AR005"])
    assert "AR005" not in rules_of(findings)


def test_parse_json_lenient():
    assert parse_json_lenient('```json\n{"findings": []}\n```') == {"findings": []}
    assert parse_json_lenient('Sure! {"summary": "x", "findings": []} hope it helps') \
        == {"summary": "x", "findings": []}
    assert parse_json_lenient('[{"rule": "a"}]') == {"findings": [{"rule": "a"}]}
    with pytest.raises(ValueError):
        parse_json_lenient("no json here")


def test_severity_parse():
    assert Severity.parse("major") == Severity.HIGH
    assert Severity.parse("CRITICAL") == Severity.CRITICAL
    assert Severity.parse("weird") == Severity.MEDIUM


def test_repo_config_cannot_redirect_host(repo, monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.delenv("ANSREVIEW_TRUST_REPO_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(repo / "nohome"))
    (repo / ".ansreview.yml").write_text("ollama_host: http://evil.example:11434\nmodel: m:1\n")
    cfg, warnings = load_config(repo)
    assert cfg.ollama_host == "http://localhost:11434"
    assert cfg.model == "m:1"
    assert any("ollama_host" in w for w in warnings)


def test_full_scan_with_mock_ollama(repo, ollama, tmp_path):
    out = tmp_path / "out"
    code = main(["scan", str(repo), "--host", ollama, "--no-lint", "-q",
                 "-f", "md,json,sarif,gitlab", "-o", str(out)])
    assert code == 1  # high findings present, default fail_on=high
    data = json.loads((out / "report.json").read_text())
    rules = {f["rule"] for f in data["findings"]}
    assert "llm:handler-never-notified" in rules
    assert "llm:maybe" not in rules        # filtered: low confidence
    assert "llm:ghost" not in rules        # filtered: file not in the unit
    assert data["model"] == "qwen2.5-coder:14b"
    sarif = json.loads((out / "report.sarif").read_text())
    assert sarif["version"] == "2.1.0" and sarif["runs"][0]["results"]
    gl = json.loads((out / "gl-code-quality-report.json").read_text())
    assert all({"fingerprint", "severity", "location"} <= set(x) for x in gl)
    assert "## Findings" in (out / "report.md").read_text()


def test_dutch_report(repo, ollama, tmp_path):
    out = tmp_path / "out"
    main(["scan", str(repo), "--host", ollama, "--no-lint", "-q", "--lang", "nl", "-o", str(out)])
    md = (out / "report.md").read_text()
    assert "## Bevindingen" in md


def test_unreachable_ollama_degrades(repo, tmp_path):
    out = tmp_path / "out"
    code = main(["scan", str(repo), "--host", "http://127.0.0.1:9", "--no-lint", "-q",
                 "-o", str(out), "--timeout", "2"])
    data = json.loads((out / "report.json").read_text())
    assert code == 1 and any("not reachable" in e for e in data["errors"])
    assert main(["scan", str(repo), "--host", "http://127.0.0.1:9", "--no-lint", "-q",
                 "-o", str(out), "--require-llm"]) == 2


def test_fail_on_threshold(repo, tmp_path):
    out = tmp_path / "out"
    assert main(["scan", str(repo), "--no-llm", "--no-lint", "-q", "-o", str(out),
                 "--fail-on", "critical"]) == 0


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


@pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
def test_diff_mode_only_reports_changed_lines(repo, tmp_path):
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    _git(repo, "branch", "-M", "main")
    _git(repo, "checkout", "-qb", "feature")
    with (repo / "playbooks" / "bootstrap.yml").open("a") as fh:
        fh.write("    - name: New task\n      ansible.builtin.copy:\n        src: a\n"
                 "        dest: /tmp/a\n        mode: \"0666\"\n")
    _git(repo, "commit", "-qam", "change")
    out = tmp_path / "out"
    main(["scan", str(repo), "--diff", "main", "--no-llm", "--no-lint", "-q", "-o", str(out)])
    data = json.loads((out / "report.json").read_text())
    assert [f["rule"] for f in data["findings"]] == ["AR006"]
