# Contributing

## Tests

```bash
pip install -e ".[dev]"
pytest
```

Tests use a mock Ollama server (`tests/mock_ollama.py`), so they need no GPU.

## Web UI

The React app is in `web/` (Vite, TypeScript). `src/ansreview/web/static/` is
its build output and is committed, so after changing `web/src/` run `make web`
and commit both. CI rebuilds it and fails when they differ (for Renovate and
Dependabot PRs it pushes the rebuilt bundle itself).

```bash
make serve        # API on :8765 (in one terminal)
make web-dev      # http://localhost:5173 with hot reload, /api proxied to :8765
make web-test     # typecheck + vitest
make web          # production build into src/ansreview/web/static/
```

## Documentation

This site is built with [Zensical](https://zensical.org) from `docs/` and
`zensical.toml`.

```bash
pip install -r requirements-docs.txt
zensical serve            # preview on http://localhost:8000
zensical build --strict   # what CI runs
```

The `Docs` workflow builds the site on every pull request that touches the docs
and fails on links to missing pages or anchors. Pushes to `main` publish it to
GitHub Pages.

## Layout

| Path | |
|---|---|
| `src/ansreview/cli.py` | Commands and flags |
| `src/ansreview/config.py` | Settings, defaults, environment variables |
| `src/ansreview/discover.py` | Finding roles and playbooks |
| `src/ansreview/lint.py` | ansible-lint |
| `src/ansreview/static.py` | The AR checks |
| `src/ansreview/review.py`, `ollama.py`, `cache.py`, `filequeue.py` | Model review, Ollama client, cache, queue |
| `src/ansreview/fix.py` | `fix` |
| `src/ansreview/report.py` | Report formats |
| `src/ansreview/gitlab.py` | `watch-mrs` |
| `src/ansreview/perf.py` | `doctor` speed check |
| `src/ansreview/web/` | `serve` and the built UI |
| `web/` | UI source |
| `deploy/systemd/` | Service units |
| `examples/` | GitLab CI, pre-commit, a sample repo |
| `scripts/` | macOS installer, Ollama server tuning |
| `docs/`, `zensical.toml` | This site |
