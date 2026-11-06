import json
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from ansreview.cli import main
from ansreview.fix import _add_no_log, _fix_mode
from ansreview.models import Finding, Severity
from ansreview.static import LineLoader, run_static

import mock_ollama

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sample-repo"
needs_git = pytest.mark.skipif(not shutil.which("git"), reason="git not installed")


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                           *args], check=True, capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path):
    dst = tmp_path / "repo"
    shutil.copytree(SAMPLE, dst)
    _git(dst, "init", "-q")
    _git(dst, "add", ".")
    _git(dst, "commit", "-qm", "init")
    return dst


@pytest.fixture()
def ollama():
    srv, url = mock_ollama.start()
    yield url
    srv.shutdown()


def _llm(rule, file, line, conf=0.9, sev="high"):
    return {"rule": f"llm:{rule}", "severity": sev, "file": file, "line": line,
            "message": rule, "source": "llm", "category": "reliability",
            "suggestion": "", "confidence": conf}


def _write_report(repo, findings):
    out = repo / ".ansreview"
    out.mkdir(exist_ok=True)
    (out / "report.json").write_text(json.dumps({"root": str(repo), "findings": findings}))


def _parses(text):
    list(yaml.load_all(text, Loader=LineLoader))
    return True


@needs_git
def test_fix_end_to_end(repo, ollama):
    _write_report(repo, [
        _llm("handler-never-notified", "roles/webserver/handlers/main.yml", 2),
        _llm("unquoted-shell-var", "roles/webserver/tasks/main.yml", 14),
        _llm("bad-edit", "site.yml", 1),
        _llm("breaks-yaml", "site.yml", 4),
        _llm("rewrite-everything", "playbooks/bootstrap.yml", 1),
        _llm("low-confidence", "site.yml", 1, conf=0.3),
    ])
    site_before = (repo / "site.yml").read_text()
    boot_before = (repo / "playbooks/bootstrap.yml").read_text()

    code = main(["fix", str(repo), "--host", ollama, "--no-lint-fix", "-q"])
    assert code == 0

    handlers = (repo / "roles/webserver/handlers/main.yml").read_text()
    assert "listen: restart nginx" in handlers
    tasks = (repo / "roles/webserver/tasks/main.yml").read_text()
    assert tasks.count("no_log: true") == 2            # mysql_user password + debug of token
    assert '"0644"' in tasks and "0777" not in tasks
    # rejected / failed model edits leave files untouched
    assert (repo / "site.yml").read_text() == site_before
    assert (repo / "playbooks/bootstrap.yml").read_text() == boot_before
    # every YAML file still parses
    for p in repo.rglob("*.yml"):
        if ".ansreview" not in p.parts:
            assert _parses(p.read_text()), p
    # the fixed issues are gone
    files = [p.relative_to(repo).as_posix() for p in repo.rglob("*.yml")
             if ".ansreview" not in p.parts]
    left, _ = run_static(repo, files)
    assert not {"AR002", "AR006", "AR015"} & {f.rule for f in left}

    report = (repo / ".ansreview" / "fix-report.md").read_text()
    assert "Declined by the model" in report and "unquoted-shell-var" in report
    assert "did not match" in report and "invalid YAML" in report and "rewrote too much" in report
    assert "low-confidence" not in report               # below threshold: never sent
    patch = (repo / ".ansreview" / "fix.patch").read_text()
    assert "+  listen: restart nginx" in patch
    assert _git(repo, "diff", "--stat").stdout.strip()   # changes visible to git


@needs_git
def test_fix_dry_run_changes_nothing(repo, ollama):
    _write_report(repo, [_llm("handler-never-notified", "roles/webserver/handlers/main.yml", 2)])
    main(["fix", str(repo), "--host", ollama, "--no-lint-fix", "--dry-run", "-q"])
    assert _git(repo, "status", "--porcelain", "--", ".", ":(exclude).ansreview").stdout == ""
    assert "listen: restart nginx" in (repo / ".ansreview" / "fix.patch").read_text()


@needs_git
def test_fix_refuses_dirty_tree(repo):
    (repo / "site.yml").write_text((repo / "site.yml").read_text() + "\n")
    assert main(["fix", str(repo), "--no-llm", "--no-lint-fix", "-q"]) == 2
    assert main(["fix", str(repo), "--no-llm", "--no-lint-fix", "-q", "--allow-dirty"]) == 0


@needs_git
@pytest.mark.skipif(not shutil.which("ansible-vault"), reason="ansible-vault not installed")
def test_fix_vaults_secrets(repo, tmp_path):
    pw = tmp_path / "vault-pass"
    pw.write_text("test-password\n")
    main(["fix", str(repo), "--no-llm", "--no-lint-fix", "-q", "--vault-password-file", str(pw)])
    defaults = (repo / "roles/webserver/defaults/main.yml").read_text()
    assert "db_password: !vault |" in defaults and "$ANSIBLE_VAULT;1.1;AES256" in defaults
    assert "Sup3rS3cret" not in defaults
    assert _parses(defaults)
    gv = (repo / "group_vars/all/main.yml").read_text()
    assert "ldap_bind_password: !vault |" in gv
    # and it decrypts back to the original value
    data = yaml.safe_load(defaults.replace("!vault", "!!str"))
    enc = data["db_password"]
    proc = subprocess.run(["ansible-vault", "decrypt", "--vault-password-file", str(pw),
                           "--output", "-"], input=enc, capture_output=True, text=True)
    assert proc.stdout == "Sup3rS3cret-example"


@needs_git
@pytest.mark.skipif(not shutil.which("ansible-lint"), reason="ansible-lint not installed")
def test_fix_runs_ansible_lint_fix(tmp_path):
    repo = tmp_path / "lintrepo"
    repo.mkdir()
    (repo / "p.yml").write_text("---\n- name: Test\n  hosts: all\n  tasks:\n"
                                "    # keep me\n    - name: Install pkg\n"
                                "      package: name=nginx state=present\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    main(["fix", str(repo), "--no-llm", "-q"])
    text = (repo / "p.yml").read_text()
    assert "ansible.builtin.package:" in text and "# keep me" in text


def test_add_no_log_on_nameless_task():
    lines = ["- ansible.builtin.user:\n", "    name: app\n", "    password: x\n",
             "- name: next\n", "  ansible.builtin.ping:\n"]
    f = Finding("AR002", Severity.MEDIUM, "t.yml", 1, "", "static", end_line=3)
    assert _add_no_log(lines, f)
    text = "".join(lines)
    assert text.startswith("- ansible.builtin.user:\n    name: app\n    password: x\n  no_log: true\n")
    data = yaml.safe_load(text)
    assert data[0]["no_log"] is True and data[0]["ansible.builtin.user"]["password"] == "x"


def test_add_no_log_flips_false():
    lines = ["- name: t\n", "  no_log: false\n", "  ansible.builtin.user:\n",
             "    password: x\n"]
    f = Finding("AR002", Severity.MEDIUM, "t.yml", 1, "", "static", end_line=4)
    _add_no_log(lines, f)
    assert yaml.safe_load("".join(lines))[0]["no_log"] is True


def test_fix_mode_directory_and_freeform():
    lines = ["- name: d\n", "  ansible.builtin.file:\n", "    path: /x\n",
             "    state: directory\n", "    mode: 0777  # wide open\n"]
    f = Finding("AR006", Severity.HIGH, "t.yml", 1, "", "static", end_line=5)
    assert _fix_mode(lines, f) == "0755"
    assert lines[4] == '    mode: "0755"  # wide open\n'
    lines = ["- name: c\n", "  copy: src=a dest=/b mode=0666\n"]
    f = Finding("AR006", Severity.HIGH, "t.yml", 1, "", "static", end_line=2)
    assert _fix_mode(lines, f) == "0644"
    assert "mode=0644" in lines[1]
