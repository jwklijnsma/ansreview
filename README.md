# ansreview

Review an Ansible codebase with three layers, all running on your own hardware:

1. **ansible-lint** for best practices, FQCN, syntax and idempotency rules.
2. **Built-in static checks (AR0xx)** for security problems linters miss: hard-coded secrets,
   missing `no_log`, `curl | sh`, world-writable modes, disabled TLS validation, SELinux/firewalld
   switched off, weak sshd settings, credentials in URLs and committed private keys.
3. **A local Ollama model** that reviews each role and playbook for logic errors, broken
   handlers, unsafe shell, idempotency problems and anything else the first two layers miss.
   It is told what the linters already found, so it looks for new problems.

No code leaves your network. Reports come out as Markdown, JSON, SARIF and GitLab Code Quality.

## Install

```bash
pip install .                 # or: pip install ".[lint]" to pull in ansible-lint too
ollama pull qwen2.5-coder:14b # any coding model works; bigger is better if VRAM allows
ansreview doctor              # checks ansible-lint, Ollama and the model
```

Requires Python 3.10+. ansible-lint is optional but recommended.

## Use

```bash
ansreview                                   # scan the current directory
ansreview ~/git/infra                       # scan another repo
ansreview --host http://gpu-box:11434       # use a remote Ollama server
ansreview --model qwen3-coder:30b --lang nl # other model, Dutch report
ansreview --diff origin/main                # only review what changed (for MRs)
ansreview --only roles/keycloak             # one role
ansreview --no-llm                          # just ansible-lint + AR checks (fast)
ansreview -f md,json,sarif,gitlab -o out/   # pick report formats and folder
```

Reports go to `<repo>/.ansreview/` by default (add it to `.gitignore`).
The exit code is `1` when a finding is at or above `--fail-on` (default `high`), `2` on a tool
error, and `0` otherwise, so it works as a pipeline gate or pre-commit hook.

### Diff mode

`--diff <ref>` reviews only the roles and playbooks that contain changed files. The model still
sees the whole role for context, but only findings on **added or modified lines** are reported
(use `--diff-scope files` to report everything in the changed files).

## Configure

`ansreview init` writes a sample `.ansreview.yml`. Settings are read in this order, later wins:
built-in defaults, `~/.ansreview.yml`, the repo's `.ansreview.yml`, environment variables, then CLI flags.

| Key | Default | Notes |
|---|---|---|
| `model` | `qwen2.5-coder:14b` | env `ANSREVIEW_MODEL` |
| `ollama_host` | `http://localhost:11434` | env `OLLAMA_HOST`; **ignored in a repo config** |
| `api_key` | – | Bearer token if Ollama sits behind an auth proxy; ignored in a repo config |
| `language` | `en` | `en` or `nl` |
| `num_ctx` | `16384` | context window per request |
| `workers` | `1` | parallel requests; raise with `OLLAMA_NUM_PARALLEL` |
| `max_chars_per_unit` | `40000` | larger roles are split into several requests |
| `fail_on` / `min_severity` | `high` / `info` | `info` `low` `medium` `high` `critical` |
| `min_confidence` | `0.5` | drop LLM findings the model is unsure about |
| `lint_profile` | – | e.g. `production` |
| `lint_offline` | `true` | `false` lets ansible-lint install `requirements.yml` collections |
| `disabled_rules` | `[]` | AR or ansible-lint rule IDs |
| `exclude` | sensible list | glob patterns |
| `house_rules` | – | free text the model enforces, e.g. naming conventions |

A repo config cannot change `ollama_host` or `api_key` unless you set
`ANSREVIEW_TRUST_REPO_CONFIG=1`. That way, a repo you cloned cannot send your code to someone
else's server.

## Built-in rules

Run `ansreview rules` for the list. Highlights:

| Rule | What |
|---|---|
| AR001 | Hard-coded secret in vars, defaults or module args (`!vault` and `{{ }}` are fine) |
| AR002 | Task passes a password/token argument without `no_log: true` |
| AR003 | `validate_certs: false` and friends |
| AR005 | `curl … \| sh` |
| AR006 | World-writable file modes |
| AR007 / AR009 / AR017 | Plain-HTTP downloads, `get_url` without checksum, `gpgcheck: false` |
| AR011 | SELinux disabled/permissive, firewalld/auditd stopped |
| AR012 | `PermitRootLogin yes` / `PasswordAuthentication yes` written to sshd_config |
| AR014 / AR016 | Credentials in URLs, private keys committed |

LLM findings use the `llm:<rule>` prefix and carry a confidence score.

## Tips

- **Missing collections.** If ansible-lint reports `syntax-check[unknown-module]`, install your
  collections first (`ansible-galaxy collection install -r requirements.yml`) or set
  `lint_offline: false`. Until then, ansible-lint skips other rules for that file.
- **Model choice.** `qwen2.5-coder:14b` fits a 16 GB card with a 16k context. With 32 GB, use
  `qwen2.5-coder:32b` or `qwen3-coder:30b` for noticeably better reviews.
- **Large repos.** Start with `--no-llm` to clear lint and AR findings cheaply, then run the model.

## CI

See `examples/gitlab-ci.yml` (Code Quality widget in merge requests) and
`examples/pre-commit-config.yaml`.

## Development

```bash
pip install -e ".[dev]"
pytest
```

Tests use a mock Ollama server (`tests/mock_ollama.py`), so they need no GPU.
