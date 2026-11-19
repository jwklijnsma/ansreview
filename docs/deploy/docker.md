# Docker

Every push to `main` publishes `ghcr.io/jwklijnsma/ansreview` (amd64 + arm64)
with ansible-lint included. Ollama is not in the image; by default it talks to
Ollama on the Docker host. Without a command the container starts the
[web UI](../usage/web-ui.md) on port 8765 for the repositories under `/repos`.

## Web UI and one-off scans

```bash
# Web UI for every repo under ~/git
docker run -d --name ansreview -p 127.0.0.1:8765:8765 \
  --add-host=host.docker.internal:host-gateway \
  --user "$(id -u):$(id -g)" -v ~/git:/repos \
  -e ANSREVIEW_WEB_TOKEN="$(openssl rand -hex 24)" \
  ghcr.io/jwklijnsma/ansreview
# the link with the token: docker logs ansreview 2>&1 | grep open:

# One-off scan of the current directory (CI, pre-merge checks)
docker run --rm --add-host=host.docker.internal:host-gateway \
  --user "$(id -u):$(id -g)" -v "$PWD:/repos/work" \
  ghcr.io/jwklijnsma/ansreview scan /repos/work
```

- `--user "$(id -u):$(id -g)"` makes reports and fixes belong to you, not root.
  The image also runs under arbitrary UIDs (OpenShift).
- On Linux, `--add-host=host.docker.internal:host-gateway` is needed to reach
  Ollama on the host. Ollama must listen on more than 127.0.0.1
  (`OLLAMA_HOST=0.0.0.0` in its service). Or set
  `-e OLLAMA_HOST=http://gpu-box:11434`.
- Pass settings with `ANSREVIEW_MODEL`, `ANSREVIEW_NUM_CTX` and so on (see
  [environment variables](../reference/configuration.md#environment-variables)),
  or mount a config:
  `-v ~/.ansreview.yml:/home/ansreview/.ansreview.yml:ro`.

## Tags

| Tag | Follows |
|---|---|
| `latest`, `main` | The main branch |
| `0.2.0`, `0.2` | Releases (see [Releasing](../development/releasing.md)) |
| `sha-abc1234` | One exact build |

## Docker Compose with Ollama on the GPU

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
      ANSREVIEW_WEB_TOKEN: ${ANSREVIEW_WEB_TOKEN:?put ANSREVIEW_WEB_TOKEN=... in .env}
    volumes: [~/git:/repos]
    depends_on: [ollama]
volumes:
  ollama:
```

Pull the model once:

```bash
docker compose exec ollama ollama pull qwen2.5-coder:14b
```

To run the container as a service that starts at boot, see
[systemd service](systemd.md).
