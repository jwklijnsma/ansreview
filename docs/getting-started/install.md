# Install

## Requirements

| What | Notes |
|---|---|
| Python 3.10+ | |
| [Ollama](https://ollama.com) | On this machine or another one with a GPU. Not needed for `--no-llm` |
| A coding model | `qwen2.5-coder:14b` by default; see [model choice](../speed.md#model-choice) |
| ansible-lint | Optional but recommended. `pip install ".[lint]"` pulls it in |
| git | For [diff mode](../usage/scan.md#diff-mode), [fix](../usage/fix.md) and [merge requests](../usage/merge-requests.md) |

## With pip

```bash
git clone https://github.com/jwklijnsma/ansreview.git && cd ansreview
pip install .                 # or: pip install ".[lint]" to pull in ansible-lint too
ollama pull qwen2.5-coder:14b # any coding model works; bigger is better if VRAM allows
ansreview doctor              # checks ansible-lint, Ollama and the model
```

`ansreview doctor` checks that ansible-lint is found, that Ollama answers, that
the model is there, and measures its speed. See
[Speed and tuning](../speed.md#1-run-ansreview-doctor) for what it reports.

## macOS (one command)

```bash
git clone https://github.com/jwklijnsma/ansreview.git && cd ansreview
./scripts/install-macos.sh --host http://<ollama-host>:11434
```

The script:

- finds a healthy Python 3.10–3.14, and installs `python@3.13` with Homebrew if needed;
- installs ansreview with ansible-lint into an isolated venv in `~/.local/share/ansreview`;
- links both tools into `~/.local/bin` and runs `ansreview doctor`.

It also works when your global Python is broken by stray `.pth` or
`sitecustomize` files (`No module named 'json'` during `pip install`). Add
`--fix-site` to move those files to a backup folder.

## Container

The image `ghcr.io/jwklijnsma/ansreview` has ansible-lint built in and needs no
Python on the host. See [Docker](../deploy/docker.md).

## Next

- [Quick start](quickstart.md): your first scan.
- [Configuration](../reference/configuration.md): point it at a remote Ollama
  server, change the model or the report language.
