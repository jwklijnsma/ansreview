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
git clone https://github.com/jwklijnsma/ansreview.git && cd ansreview
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
ansreview serve --root ~/git                # web UI (see below)
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

**Speed.** Each file gets one model request with all of its findings (set `fix_batch_size`
to cap that). Files longer
than `fix_full_file_lines` are sent as excerpts around each finding, not as a whole, which cuts
prompt time on big playbooks. Responses are streamed, so `timeout` is the longest the model may
stay silent, not the total time. A batch that times out is split in half and tried again; it is
never resent unchanged. Answers and timeouts are cached in `.ansreview/fix-cache.json`, so a
re-run only asks about files that changed. Use `--no-cache` to ask again.

## Web UI

```
ansreview serve --root ~/git                    # a folder of repos (or one repo)
ansreview serve --root ~/git/infra --root ~/git/platform --open
```

It prints a link with an access token (`http://localhost:8765/?token=…`). Open it once; the token becomes a cookie. From the browser you can:

- pick a repository and run a **scan**, with the same options as the CLI (diff mode, only some roles, model override, skip the model, lint profile);
- let it run in the **background**: scans and fixes go into a queue on the server and keep running when you close the page. The **Jobs** button in the top bar shows what is running, what is waiting (with its place in the queue) and what finished, with progress, the log, Cancel/Remove, and download buttons;
- browse findings by severity, source (AR / ansible-lint / model) or rule, with the suggested fix and the code around each line;
- **Preview fixes** (`fix --dry-run`) and read the fix report and colour diff, then **Apply fixes**. Nothing is ever committed; review with `git diff` as usual.
- **Upload a ZIP** of an Ansible repo (button or drag and drop). It is unpacked and its scan is queued straight away. **Fix & download** fixes it in the background; when it is done you get a notification with the fixed ZIP. A GitHub "Download ZIP" works as-is.
- **download** the results: every finished job offers its files (fixed ZIP, `fix.patch`, fix report, report as Markdown, JSON or SARIF), and each repo has a **Downloads** tab with the latest ones.

Scans and fixes run as `ansreview scan|fix` subprocesses, so they behave exactly like the CLI and write the same `.ansreview/` reports. Jobs run one at a time, in order, since they share the GPU; up to 20 can wait. The job list lives in the server process, so a restart of `ansreview serve` forgets it (the reports on disk stay).

The UI is a React app (`web/`), built into `src/ansreview/web/static/` and shipped inside the Python package, so a normal install needs no Node and nothing is loaded from a CDN: it works on an air-gapped host.

**Uploads** are unpacked into `~/.cache/ansreview/uploads` (`--upload-dir`), up to 100 MB per ZIP (`--max-upload-mb`), and deleted after 7 days (`--upload-keep-days`, or the Delete button). Each one becomes its own small git repository, so `fix` works on it. Extraction refuses paths that leave the folder, skips symlinks and any `.git`, and stops ZIP bombs (size, file count and compression ratio are checked on the bytes actually written). Note that ansible-lint runs on the uploaded code just as on a repo under `--root`, so only share the token with people you would let run ansible-lint on this host. `--no-upload` switches uploads off.

**Security.** By default it listens on `127.0.0.1` only and every API call needs the token (`--token` or `$ANSREVIEW_WEB_TOKEN` to fix it, otherwise a random one per start). Only repositories under the `--root` folders are reachable, and file reads cannot leave a repo. The Ollama host, output folder, config file and vault password file cannot be set from the browser. POSTs need a custom header, so other sites cannot trigger a scan. `--no-auth` is refused unless you bind to localhost. To reach it from another machine, prefer an SSH tunnel (`ssh -L 8765:localhost:8765 gpu-box`). If you do use `--bind 0.0.0.0`, put it behind a TLS reverse proxy, because the token travels in plain HTTP otherwise.

## Docker

Every push to `main` publishes `ghcr.io/jwklijnsma/ansreview` (amd64 + arm64) with ansible-lint included. Ollama is not in the image; by default it talks to Ollama on the Docker host.

```bash
# Web UI for every repo under ~/git
docker run -d --name ansreview -p 127.0.0.1:8765:8765 \
  --add-host=host.docker.internal:host-gateway \
  --user "$(id -u):$(id -g)" -v ~/git:/repos \
  -e ANSREVIEW_WEB_TOKEN=change-me \
  ghcr.io/jwklijnsma/ansreview
# then open http://localhost:8765/?token=change-me

# One-off scan of the current directory (CI, pre-merge checks)
docker run --rm --add-host=host.docker.internal:host-gateway \
  --user "$(id -u):$(id -g)" -v "$PWD:/repos/work" \
  ghcr.io/jwklijnsma/ansreview scan /repos/work
```

- `--user "$(id -u):$(id -g)"` makes reports and fixes belong to you, not root. The image also runs under arbitrary UIDs (OpenShift).
- On Linux, `--add-host=host.docker.internal:host-gateway` is needed to reach Ollama on the host. Ollama must listen on more than 127.0.0.1 (`OLLAMA_HOST=0.0.0.0` in its service). Or set `-e OLLAMA_HOST=http://gpu-box:11434`.
- Pass settings with `ANSREVIEW_MODEL`, `ANSREVIEW_NUM_CTX` and so on, or mount a config: `-v ~/.ansreview.yml:/home/ansreview/.ansreview.yml:ro`.
- Tags: `latest` and `main` follow the main branch, `0.2.0`/`0.2` come from releases (`make release`), and `sha-abc1234` pins an exact build.

With Ollama on the GPU in the same compose project:

```yaml
services:
  ollama:
    image: ollama/ollama
    volumes: [ollama:/root/.ollama]
    deploy:
      resources:
        reservations:
          devices: [{ driver: nvidia, count: all, capabilities: [gpu] }]
  ansreview:
    image: ghcr.io/jwklijnsma/ansreview
    user: "1000:1000"            # your uid:gid
    ports: ["127.0.0.1:8765:8765"]
    environment:
      OLLAMA_HOST: http://ollama:11434
      ANSREVIEW_WEB_TOKEN: change-me
    volumes: [~/git:/repos]
    depends_on: [ollama]
volumes:
  ollama:
```

Pull the model once with `docker compose exec ollama ollama pull qwen2.5-coder:14b`.

## Releasing

Releases run on GitHub. In the repository: **Actions → release → Run workflow**, pick `patch`, `minor` or `major` (or type an exact version), and start it. Or from a terminal with the [GitHub CLI](https://cli.github.com):

```bash
make release                    # 0.2.0 -> 0.2.1, then follows the run
make release BUMP=minor         # 0.2.0 -> 0.3.0   (or BUMP=major)
make release VERSION=1.0.0      # an exact version
make release-dry-run            # rehearse: test and build everything, publish nothing
```

The workflow:
1. checks the new version is higher and not released yet, and runs the tests;
2. bumps `__version__` in `src/ansreview/__init__.py` (the only place the version lives), commits `release: vX.Y.Z` to `main`, and pushes it together with an annotated `vX.Y.Z` tag;
3. builds the wheel and sdist and creates the GitHub release with install lines and notes generated from the merged PRs;
4. builds and smoke-tests the Docker image and publishes `ghcr.io/jwklijnsma/ansreview:X.Y.Z`, `:X.Y` and `:latest`.

A dry run does all of this without the push, the release or the image push, and shows the release notes it would use in the run summary. If you ever push a `vX.Y.Z` tag by hand, it still gets a GitHub release and an image.

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
| `per_file` | `true` | one model request per file |
| `max_chars_per_unit` | `24000` | larger roles are split into several requests |
| `fail_on` / `min_severity` | `high` / `info` | `info` `low` `medium` `high` `critical` |
| `min_confidence` | `0.5` | drop LLM findings the model is unsure about |
| `lint_profile` | – | e.g. `production` |
| `lint_offline` | `true` | `false` lets ansible-lint install `requirements.yml` collections |
| `fix_min_severity` / `fix_min_confidence` | `medium` / `0.8` | which model findings `fix` may touch |
| `fix_batch_size` | `0` | findings per request in `fix`; `0` = all of a file at once (`--batch-size`) |
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

## Speed

Both `scan` and `fix` send **one request per file** and work through the files in order:
`[12/65] ✓ roles/docker/tasks/main.yml 41s`. The status of every file (pending, running,
done, cached, failed) is kept in `.ansreview/queue.json`. To watch a long run from another
terminal, use `jq '.counts' .ansreview/queue.json`. Set `per_file: false` to send a whole
role in one request again.

A local model will never match a cloud service, but most slow runs come from a misconfigured
server rather than from the GPU itself. In order of impact:

1. **Run `ansreview doctor`.** It measures prompt and generation speed and estimates how long
   one fix batch and one scan chunk take. It also flags the two big problems:
   - part of the model running on the CPU (often 5–10× slower);
   - the model reloading between requests because another app on the same server uses a
     different model or `num_ctx`.
2. **Tune the server.** Run `sudo scripts/tune-ollama-server.sh --parallel 2` on the Ollama
   host. It turns on flash attention, a q8 KV cache, parallel slots and keep-alive. Then run
   ansreview with `--workers 2`, or set it in `~/.ansreview.yml`.
3. **Re-runs are cheap.** Model answers are cached in `.ansreview/scan-cache.json` and
   `fix-cache.json`, so only changed files go back to the model. Requests that timed out are
   remembered too and are split straight away next time.
4. **Use a smaller model for `fix`.** `ansreview fix --model qwen2.5-coder:7b` is about twice
   as fast. Fixes are small and every edit is verified anyway.

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

### Web UI

The React app is in `web/` (Vite, TypeScript). `src/ansreview/web/static/` is its build output and is committed, so after changing `web/src/` run `make web` and commit both. CI rebuilds it and fails when they differ (for Renovate/Dependabot PRs it pushes the rebuilt bundle itself).

```bash
make serve        # API on :8765 (in one terminal)
make web-dev      # http://localhost:5173 with hot reload, /api proxied to :8765
make web-test     # typecheck + vitest
make web          # production build into src/ansreview/web/static/
```
