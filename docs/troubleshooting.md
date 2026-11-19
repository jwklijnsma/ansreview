# Troubleshooting

Start with `ansreview doctor` (add `--host` for a remote Ollama). It checks
ansible-lint, Ollama, the model and its speed.

## Symptoms

| Symptom | Likely cause | Fix |
|---|---|---|
| `syntax-check[unknown-module]` from ansible-lint | Collections in `requirements.yml` are not installed. Until they are, ansible-lint skips other rules for that file | `ansible-galaxy collection install -r requirements.yml`, or set `lint_offline: false` |
| `warning: Ollama not reachable … LLM review skipped` | Wrong host, Ollama only listens on 127.0.0.1, or a firewall | Check `--host` / `OLLAMA_HOST`. On the Ollama host set `OLLAMA_HOST=0.0.0.0` in its service. Use `--require-llm` to make this an error |
| Container can't reach Ollama on the host | `host.docker.internal` is not defined on Linux | Add `--add-host=host.docker.internal:host-gateway`, or set `OLLAMA_HOST` to the host's address |
| Every file takes minutes | Part of the model runs on the CPU, or the model reloads between requests | See [Speed and tuning](speed.md) |
| Requests time out | Model too big for the GPU, or too many `workers` for the server's parallel slots | Lower `workers`, use a smaller model, or raise `timeout` |
| `ollama_host` in the repo's `.ansreview.yml` is ignored | On purpose: a cloned repo must not send your code to another server | Put it in `~/.ansreview.yml`, the environment or `--host`; or set `ANSREVIEW_TRUST_REPO_CONFIG=1` |
| `fix` refuses to run | The repo has uncommitted changes | Commit or stash them, or pass `--allow-dirty` |
| A fix was not applied | The edit did not match exactly once, broke the YAML or rewrote too much | See `.ansreview/fix-report.md`; it says why |
| Too many findings | Low-value rules for your repo | `--min-severity medium`, `disabled_rules`, or `--min-confidence` for model findings |
| `No module named 'json'` during `pip install` on macOS | Stray `.pth` / `sitecustomize` files in the global Python | Use `scripts/install-macos.sh --fix-site` (see [Install](getting-started/install.md#macos-one-command)) |
| The web UI says the token is wrong | The token changed at a restart (none was set) | Set `--token` or `$ANSREVIEW_WEB_TOKEN`, or read the new link from the log |
| The service doesn't start | The token in the settings file is still the example value | Set a real token; see [systemd service](deploy/systemd.md) |
| A merge request gets no comment | It changes nothing ansreview reviews, is a draft or fork you skip, or is older than `gitlab_max_age_days` | `ansreview watch-mrs … --dry-run` lists what would be reviewed |
| GitLab calls fail with certificate errors | Internal CA | Set `REQUESTS_CA_BUNDLE` and `GIT_SSL_CAINFO` to your CA bundle |

## Tips

- **Large repos.** Start with `--no-llm` to clear lint and AR findings cheaply,
  then run the model.
- **Only a branch's changes.** `--diff origin/main` reviews only the changed
  lines; see [diff mode](usage/scan.md#diff-mode).
- **Ask the model again.** Answers are cached; `--no-cache` ignores the cache.
- **Where are the logs?** CLI runs print to the terminal. Services log to the
  journal: `journalctl [--user] -u ansreview -f`.
