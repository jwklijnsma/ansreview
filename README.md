# ansreview

[![Docker](https://github.com/jwklijnsma/ansreview/actions/workflows/docker.yml/badge.svg)](https://github.com/jwklijnsma/ansreview/actions/workflows/docker.yml)
[![Docs](https://github.com/jwklijnsma/ansreview/actions/workflows/docs.yml/badge.svg)](https://jwklijnsma.github.io/ansreview/)

Review an Ansible codebase with three layers, all running on your own hardware:

1. **ansible-lint** for best practices, FQCN, syntax and idempotency rules.
2. **Built-in static checks (AR0xx)** for security problems linters miss: hard-coded secrets,
   missing `no_log`, `curl | sh`, world-writable modes, disabled TLS validation, SELinux/firewalld
   switched off, weak sshd settings, credentials in URLs and committed private keys.
3. **A local Ollama model** that reviews each role and playbook for logic errors, broken
   handlers, unsafe shell, idempotency problems and anything else the first two layers miss.
   It is told what the linters already found, so it looks for new problems.

No code leaves your network. Reports come out as Markdown, JSON, SARIF and GitLab Code Quality.

📖 **Documentation: <https://jwklijnsma.github.io/ansreview/>**

## Features

- **Scan** a repo, or only what a branch changed (`--diff origin/main`).
- **Fix** what it found: model edits that are verified first, built-in fixes (`no_log`, file
  modes, vaulting secrets) and `ansible-lint --fix`. Nothing is ever committed.
- **Web UI** (`ansreview serve`): scan, browse findings, preview and apply fixes, upload a ZIP,
  with a job queue on the server. Works air-gapped.
- **GitLab merge requests** (`ansreview watch-mrs`): polls GitLab and keeps one comment per
  merge request up to date. No CI change in your repos.
- **Container and service**: `ghcr.io/jwklijnsma/ansreview`, with ready-made Podman Quadlet
  and Docker systemd units.

## Quick start

```bash
git clone https://github.com/jwklijnsma/ansreview.git && cd ansreview
pip install ".[lint]"         # Python 3.10+, with ansible-lint
ollama pull qwen2.5-coder:14b # any coding model works; bigger is better if VRAM allows
ansreview doctor              # checks ansible-lint, Ollama and the model

cd ~/git/infra
ansreview --host http://gpu-box:11434   # reports in .ansreview/
ansreview fix --dry-run                 # .ansreview/fix.patch, changes nothing
```

On macOS, `./scripts/install-macos.sh --host http://<ollama-host>:11434` does the install in
one command.

## Commands

| Command | |
|---|---|
| `ansreview [scan] [path]` | Review the codebase (default command) |
| `ansreview fix [path]` | Fix findings in your working tree; review with `git diff` |
| `ansreview serve --root ~/git` | Web UI on port 8765 |
| `ansreview watch-mrs --project group/repo` | Review GitLab merge requests as they come in |
| `ansreview doctor` | Check ansible-lint, Ollama, the model and its speed |
| `ansreview rules` | List the built-in AR rules |
| `ansreview init` | Write a sample `.ansreview.yml` |

The exit code is `1` when a finding is at or above `--fail-on` (default `high`), `2` on a tool
error, and `0` otherwise, so it works as a pipeline gate or pre-commit hook.

## Documentation

| | |
|---|---|
| [Install](https://jwklijnsma.github.io/ansreview/getting-started/install/) · [Quick start](https://jwklijnsma.github.io/ansreview/getting-started/quickstart/) | Requirements, pip, macOS, container |
| [Scan](https://jwklijnsma.github.io/ansreview/usage/scan/) · [Automatic fixes](https://jwklijnsma.github.io/ansreview/usage/fix/) | Diff mode, exit codes, the three fix stages |
| [Web UI](https://jwklijnsma.github.io/ansreview/usage/web-ui/) · [GitLab merge requests](https://jwklijnsma.github.io/ansreview/usage/merge-requests/) | Jobs, uploads, security; the MR watcher |
| [Docker](https://jwklijnsma.github.io/ansreview/deploy/docker/) · [systemd](https://jwklijnsma.github.io/ansreview/deploy/systemd/) · [CI](https://jwklijnsma.github.io/ansreview/deploy/ci/) | Container, service units, GitLab CI and pre-commit |
| [Speed and tuning](https://jwklijnsma.github.io/ansreview/speed/) · [Troubleshooting](https://jwklijnsma.github.io/ansreview/troubleshooting/) | `doctor`, Ollama tuning, model choice, common problems |
| [Configuration](https://jwklijnsma.github.io/ansreview/reference/configuration/) · [Rules](https://jwklijnsma.github.io/ansreview/reference/rules/) · [Commands](https://jwklijnsma.github.io/ansreview/reference/cli/) | Every setting, rule and flag |
| [Contributing](https://jwklijnsma.github.io/ansreview/development/contributing/) · [Releasing](https://jwklijnsma.github.io/ansreview/development/releasing/) | Tests, web UI build, docs, releases |

The site is built from [`docs/`](docs/) with [Zensical](https://zensical.org):
`pip install -r requirements-docs.txt && zensical serve`.

## Development

```bash
pip install -e ".[dev]"
pytest
```

Tests use a mock Ollama server (`tests/mock_ollama.py`), so they need no GPU. See
[Contributing](https://jwklijnsma.github.io/ansreview/development/contributing/) for the web UI.
