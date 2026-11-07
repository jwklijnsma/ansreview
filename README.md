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

### macOS (one command)

```bash
git clone https://github.com/jwklijnsma/-Ansreview.git && cd -- -Ansreview
./scripts/install-macos.sh --host http://<ollama-host>:11434
```

The script finds a healthy Python 3.10–3.14 (it installs `python@3.13` via Homebrew if needed)
and installs ansreview with ansible-lint into an isolated venv in `~/.local/share/ansreview`.
It then links both tools into `~/.local/bin` and runs `ansreview doctor`. It also works when your
global Python is broken by stray `.pth`/`sitecustomize` files (`No module named 'json'` during
`pip install`). Add `--fix-site` to move those files to a backup folder.

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

## Automatic fixes

```bash
ansreview fix ~/git/infra                       # uses .ansreview/report.json from your last scan
ansreview fix ~/git/infra --dry-run             # only write .ansreview/fix.patch
ansreview fix ~/git/infra --only roles/keycloak # one role
ansreview fix ~/git/infra --vault-password-file ~/.vault-pass   # also vault hard-coded secrets
```

`fix` works in a scratch copy of the repo, in three stages:

1. **Model fixes.** For every model finding at or above `medium` with confidence of at least 0.8, the
   model first checks whether the finding is real. False positives, such as `inventory_hostname`
   in `command:` flagged as injection, are declined. Real findings get minimal exact
   search/replace edits. An edit is rejected if it doesn't match the file exactly once, breaks
   the YAML, or rewrites too much of the file.
2. **Built-in fixes.** These are deterministic and use fresh line numbers. They add
   `no_log: true` (AR002, AR015), set world-writable modes to `0644` (or `0755` for
   directories) (AR006), and encrypt hard-coded secrets in place with ansible-vault when you pass
   `--vault-password-file` (AR001). Risky changes such as sshd settings, SELinux, http→https and
   checksums are listed under **Needs a human** and left alone.
3. **`ansible-lint --fix`** for mechanical rules: FQCN, free-form arguments, name casing and
   YAML formatting. Comments are kept. Limit it with `--lint-fix-rules fqcn,name`, or skip it with
   `--no-lint-fix`.

Every changed YAML file must still parse, or it is reverted. Results are written to your working
tree but **never committed**. Review them with `git diff` and undo with `git checkout -- .`.
`.ansreview/fix-report.md` lists what was fixed, declined, failed or needs a human, and
`.ansreview/fix.patch` holds the diff. `fix` refuses to run on a repo with uncommitted changes
(override with `--allow-dirty`), so the diff only ever shows ansreview's work.

Vaulting a secret does not remove it from git history, so **rotate it**.

**Speed.** Model fixes are sent in batches of `fix_batch_size` findings (default 4). Files longer
than `fix_full_file_lines` are sent as excerpts around each finding, not as a whole, which cuts
prompt time on big playbooks. Responses are streamed, so `timeout` is the longest the model may
stay silent, not the total time. A batch that times out is split in half and tried again; it is
never resent unchanged. Answers and timeouts are cached in `.ansreview/fix-cache.json`, so a
re-run only asks about files that changed. Use `--no-cache` to ask again.

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
| `workers` | `1` | parallel requests; keep it at or below the server's `OLLAMA_NUM_PARALLEL` (queued requests count against `timeout`) |
| `timeout` | `600` | max seconds without model output; env `ANSREVIEW_TIMEOUT` |
| `deadline` | `0` | max seconds per request (`0` = 2 × `timeout`) |
| `num_predict` | `4096` | cap on generated tokens; stops a model stuck repeating JSON |
| `keep_alive` | `30m` | keeps the model loaded between requests |
| `max_chars_per_unit` | `40000` | larger roles are split into several requests |
| `fail_on` / `min_severity` | `high` / `info` | `info` `low` `medium` `high` `critical` |
| `min_confidence` | `0.5` | drop LLM findings the model is unsure about |
| `lint_profile` | – | e.g. `production` |
| `lint_offline` | `true` | `false` lets ansible-lint install `requirements.yml` collections |
| `fix_min_severity` / `fix_min_confidence` | `medium` / `0.8` | which model findings `fix` may touch |
| `fix_batch_size` | `4` | findings per model request in `fix` (`--batch-size`) |
| `fix_context_lines` / `fix_full_file_lines` | `25` / `120` | excerpt size for long files in `fix` |
| `lint_fix_rules` | `all` | write list for `ansible-lint --fix` |
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
