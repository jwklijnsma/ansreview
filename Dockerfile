# syntax=docker/dockerfile:1
#
# ansreview: ansible-lint + AR checks + a local Ollama model, with the web UI.
#
#   docker run -d -p 127.0.0.1:8765:8765 \
#     --add-host=host.docker.internal:host-gateway \
#     --user "$(id -u):$(id -g)" -v ~/git:/repos \
#     -e ANSREVIEW_WEB_TOKEN=change-me ghcr.io/jwklijnsma/ansreview
#
#   docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/repos/work" \
#     ghcr.io/jwklijnsma/ansreview scan /repos/work
#
# Ollama is not in this image; point OLLAMA_HOST at it (default: the Docker host).

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------- build
FROM python:${PYTHON_VERSION}-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# ".[lint]" brings ansible-lint (and ansible-core) along, so lint findings work out of the box.
# DL3013: this installs the local project; its dependency ranges live in pyproject.toml.
# hadolint ignore=DL3013
RUN pip install ".[lint]" \
 && ansreview --version \
 && ansible-lint --version

# ---------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim

ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="ansreview" \
      org.opencontainers.image.description="Review Ansible code with ansible-lint, static security checks and a local Ollama model" \
      org.opencontainers.image.source="https://github.com/jwklijnsma/-Ansreview" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

# - git: diff mode, the dirty-tree guard and `fix`; tini: a proper PID 1 (signals, zombies).
# - safe.directory: mounted repos belong to the host user, not to whoever runs in the
#   container, and git would refuse them as "dubious ownership".
# - $HOME is writable for any UID (docker --user, OpenShift's random UIDs), because
#   ansible-lint keeps a cache there.
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends git tini ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system --add safe.directory '*' \
 && useradd --uid 1000 --gid 0 --home-dir /home/ansreview --create-home --shell /usr/sbin/nologin ansreview \
 && chmod 1777 /home/ansreview \
 && mkdir -p /repos \
 && chmod 0775 /repos && chgrp 0 /repos

COPY --from=build /opt/venv /opt/venv

ENV PATH=/opt/venv/bin:$PATH \
    HOME=/home/ansreview \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ANSIBLE_LOCAL_TEMP=/tmp/.ansible-tmp \
    ANSREVIEW_CONTAINER=1 \
    OLLAMA_HOST=http://host.docker.internal:11434

USER 1000:0
WORKDIR /repos
EXPOSE 8765

# Only meaningful for `serve`; one-shot `scan`/`fix` runs exit before the first check.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8765/', timeout=4); sys.exit(0)"]

# SIGINT lets `serve` cancel running jobs and shut down cleanly on `docker stop`.
STOPSIGNAL SIGINT
ENTRYPOINT ["tini", "--", "ansreview"]
CMD ["serve", "--root", "/repos", "--bind", "0.0.0.0", "--port", "8765"]
