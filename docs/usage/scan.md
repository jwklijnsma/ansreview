# Scan

`scan` is the default command: `ansreview` and `ansreview scan` do the same.

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

## What happens

1. **ansible-lint** runs on the repo, with your `lint_profile` if you set one.
2. The **built-in AR checks** run on every YAML file. See [Built-in rules](../reference/rules.md).
3. The **model** gets one request per file (or per role with `per_file: false`),
   with what the first two layers already found, so it looks for new problems.
   Large roles are split into several requests (`max_chars_per_unit`).
4. Findings below `min_severity`, model findings below `min_confidence`, and
   rules in `disabled_rules` are dropped.
5. The reports are written to `<repo>/.ansreview/`. See [Reports](../reference/reports.md).

Each layer can be switched off: `--no-lint`, `--no-static`, `--no-llm`. When
Ollama can't be reached, the scan carries on without the model and says so;
`--require-llm` makes that an error instead.

## Diff mode

```bash
ansreview --diff origin/main
```

`--diff <ref>` reviews only the roles and playbooks that contain changed files.
The model still sees the whole role for context, but only findings on **added
or modified lines** are reported. Use `--diff-scope files` to report everything
in the changed files.

This is what the [merge-request reviewer](merge-requests.md) and the
[CI job](../deploy/ci.md) use. In CI, fetch enough history for the ref to
exist (`GIT_DEPTH: "0"` in GitLab).

## Limit the scan

| Flag | Effect |
|---|---|
| `--only roles/nginx` | Only units or paths that match (repeatable) |
| `--exclude 'molecule/**'` | Skip paths (repeatable; adds to `exclude`) |
| `--disable AR006` | Drop a rule (repeatable; adds to `disabled_rules`) |
| `--min-severity medium` | Drop findings below this severity |
| `--min-confidence 0.7` | Drop model findings the model is less sure about |

## Exit code

| Code | Meaning |
|---|---|
| `0` | No finding at or above `--fail-on` |
| `1` | At least one finding at or above `--fail-on` (default `high`) |
| `2` | Tool error (bad config, `--require-llm` without Ollama, …) |

So a scan works as a pipeline gate or a pre-commit hook.

## Cache

Model answers are cached in `.ansreview/scan-cache.json`. A re-run only sends
files that changed. Requests that timed out are remembered too, and are split
straight away next time. `--no-cache` asks the model again for everything.

## Watching a long run

The status of every file (pending, running, done, cached, failed) is kept in
`.ansreview/queue.json`. From another terminal:

```bash
jq '.counts' .ansreview/queue.json
```

All flags are listed under [Commands](../reference/cli.md#scan).
