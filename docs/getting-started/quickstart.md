# Quick start

## 1. Check the setup

```bash
ansreview doctor --host http://gpu-box:11434
```

Leave out `--host` when Ollama runs on the same machine.

## 2. Scan a repository

```bash
cd ~/git/infra
ansreview --host http://gpu-box:11434
```

Every file is reviewed in turn, with progress like
`[12/65] ✓ roles/docker/tasks/main.yml 41s`. The reports land in
`.ansreview/` in the repo. Add that folder to `.gitignore`.

!!! tip "Start fast"
    On a big repo, run `ansreview --no-llm` first. It runs only ansible-lint and
    the built-in checks and takes seconds. Clear those findings, then run the model.

## 3. Read the report

Open `.ansreview/report.md`, or look at the summary in the terminal. Every
finding has a severity, a rule, a file and line, and a suggested fix. Model
findings start with `llm:` and carry a confidence score.

## 4. Fix what it found

```bash
ansreview fix --dry-run     # writes .ansreview/fix.patch, changes nothing
ansreview fix               # applies the fixes to your working tree
git diff                    # review; undo with git checkout -- .
```

Nothing is committed. See [Automatic fixes](../usage/fix.md).

## 5. Save your settings

```bash
ansreview init              # writes a sample .ansreview.yml
```

Put settings that are about you (Ollama host, model) in `~/.ansreview.yml`, and
settings that are about the repo (excluded paths, house rules) in the repo's
`.ansreview.yml`. See [Configuration](../reference/configuration.md).

## Where next

- Only review what a branch changed: [diff mode](../usage/scan.md#diff-mode).
- Work from the browser: [Web UI](../usage/web-ui.md).
- Review merge requests automatically: [GitLab merge requests](../usage/merge-requests.md).
