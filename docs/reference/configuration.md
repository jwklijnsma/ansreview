# Configuration

`ansreview init` writes a sample `.ansreview.yml`. Settings are read in this
order, later wins:

1. built-in defaults
2. `~/.ansreview.yml`
3. the repo's `.ansreview.yml` (or `.ansreview.yaml`)
4. an extra file given with `-c`
5. environment variables
6. CLI flags

Unknown keys are reported as a warning and ignored.

## Example

```yaml
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
```

## Model and Ollama

| Key | Default | Notes |
|---|---|---|
| `model` | `qwen2.5-coder:14b` | env `ANSREVIEW_MODEL`, `-m` |
| `ollama_host` | `http://localhost:11434` | env `OLLAMA_HOST`, `--host`; **ignored in a repo config** |
| `api_key` | – | Bearer token if Ollama sits behind an auth proxy; ignored in a repo config |
| `num_ctx` | `16384` | Context window per request |
| `workers` | `1` | Parallel requests; keep it at or below the server's `OLLAMA_NUM_PARALLEL` (queued requests count against `timeout`) |
| `timeout` | `600` | Max seconds without model output; env `ANSREVIEW_TIMEOUT` |
| `deadline` | `0` | Max seconds per request (`0` = 2 × `timeout`) |
| `num_predict` | `4096` | Cap on generated tokens; stops a model stuck repeating JSON |
| `keep_alive` | `30m` | Keeps the model loaded between requests |
| `temperature` | `0.1` | |
| `retries` | `2` | |

## Scan

| Key | Default | Notes |
|---|---|---|
| `language` | `en` | Report language: `en` or `nl` |
| `per_file` | `true` | One model request per file; `false` groups a role's files |
| `max_chars_per_unit` | `24000` | Larger roles are split into several requests |
| `max_file_chars` | `20000` | Single huge files are truncated |
| `fail_on` | `high` | Exit `1` at or above: `info` `low` `medium` `high` `critical` |
| `min_severity` | `info` | Hide findings below this |
| `min_confidence` | `0.5` | Drop model findings the model is unsure about |
| `lint_profile` | – | ansible-lint profile, e.g. `production` |
| `lint_offline` | `true` | `false` lets ansible-lint install `requirements.yml` collections |
| `disabled_rules` | `[]` | AR or ansible-lint rule IDs |
| `exclude` | sensible list | Glob patterns: `.git`, venvs, `node_modules`, installed collections, molecule caches, … |
| `house_rules` | – | Free text the model enforces, e.g. naming conventions |

## Fix

| Key | Default | Notes |
|---|---|---|
| `fix_min_severity` / `fix_min_confidence` | `medium` / `0.8` | Which model findings `fix` may touch |
| `fix_batch_size` | `0` | Findings per request; `0` = all of a file at once (`--batch-size`) |
| `fix_context_lines` / `fix_full_file_lines` | `25` / `120` | Excerpt size for long files |
| `lint_fix_rules` | `all` | Write list for `ansible-lint --fix` (`all`, `none` or rule IDs) |

## GitLab merge requests

Only honoured from `~/.ansreview.yml`, the environment and flags, never from a
repo config. See [GitLab merge requests](../usage/merge-requests.md#settings).

| Key | Default |
|---|---|
| `gitlab_url` | – |
| `gitlab_projects` / `gitlab_groups` | `[]` |
| `gitlab_token_env` | `GITLAB_TOKEN` |
| `gitlab_interval` | `120` |
| `gitlab_skip_drafts` / `gitlab_skip_forks` | `false` |
| `gitlab_max_age_days` | `14` |

## Environment variables

| Variable | Key |
|---|---|
| `OLLAMA_HOST`, `ANSREVIEW_OLLAMA_HOST` | `ollama_host` |
| `ANSREVIEW_MODEL` | `model` |
| `ANSREVIEW_API_KEY` | `api_key` |
| `ANSREVIEW_LANGUAGE` | `language` |
| `ANSREVIEW_WORKERS` | `workers` |
| `ANSREVIEW_NUM_CTX` | `num_ctx` |
| `ANSREVIEW_TIMEOUT` | `timeout` |
| `ANSREVIEW_NUM_PREDICT` | `num_predict` |
| `ANSREVIEW_FIX_BATCH_SIZE` | `fix_batch_size` |
| `ANSREVIEW_FAIL_ON` | `fail_on` |
| `ANSREVIEW_GITLAB_URL` | `gitlab_url` |
| `ANSREVIEW_GITLAB_TOKEN_ENV` | `gitlab_token_env` |
| `ANSREVIEW_GITLAB_PROJECTS` / `ANSREVIEW_GITLAB_GROUPS` | comma-separated |
| `ANSREVIEW_GITLAB_INTERVAL` | `gitlab_interval` |
| `ANSREVIEW_GITLAB_SKIP_DRAFTS` / `ANSREVIEW_GITLAB_SKIP_FORKS` | |

Other variables:

| Variable | |
|---|---|
| `ANSREVIEW_TRUST_REPO_CONFIG=1` | Let a repo config set `ollama_host`, `api_key` and the `gitlab_*` keys |
| `ANSREVIEW_WEB_TOKEN` | Access token for the [web UI](../usage/web-ui.md) |
| `ANSREVIEW_WATCH_MRS=1` | `serve` also reviews merge requests (container) |
| `GITLAB_TOKEN` | GitLab token, or the variable named in `gitlab_token_env` |
| `REQUESTS_CA_BUNDLE`, `GIT_SSL_CAINFO` | Internal CA for GitLab |

## Repo configs are not trusted

A repo config cannot change `ollama_host`, `api_key` or the `gitlab_*` keys
unless you set `ANSREVIEW_TRUST_REPO_CONFIG=1`. That way, a repo you cloned
cannot send your code to someone else's server.
