# systemd service

Ready-made units are in
[`deploy/systemd/`](https://github.com/jwklijnsma/ansreview/tree/main/deploy/systemd).
They start the container at boot, restart it on failure, log to the journal and
keep uploads in a volume.

=== "Podman Quadlet (rootless)"

    For RHEL and Fedora. Runs as the user who owns the repos.

    ```bash
    openssl rand -hex 24 | tr -d '\n' | podman secret create ansreview-token -   # the access token
    mkdir -p ~/.config/containers/systemd ~/.config/ansreview
    cp deploy/systemd/ansreview.container ~/.config/containers/systemd/
    install -m 600 deploy/systemd/ansreview.env.example ~/.config/ansreview/ansreview.env   # must exist
    systemctl --user daemon-reload
    systemctl --user start ansreview
    loginctl enable-linger "$USER"                           # keeps running without a login
    systemctl --user enable --now podman-auto-update.timer   # pulls new images daily
    ```

    - Show the token again with
      `podman secret inspect --showsecret --format '{{.SecretData}}' ansreview-token`.
    - Settings (model, context size) go in `~/.config/ansreview/ansreview.env`.
      The file must exist; an unchanged copy of `ansreview.env.example` is fine.
    - The repositories come from `~/git`. Change the
      `Volume=%h/git:/repos:z` line for another folder.
    - Reports and fixes stay owned by you (`UserNS=keep-id`).
    - Ollama on the same host is reached as `host.containers.internal`, so it
      must listen on more than 127.0.0.1
      (`systemctl edit ollama` → `Environment=OLLAMA_HOST=0.0.0.0`). For Ollama
      on another machine, change the unit's `Environment=OLLAMA_HOST=` line.

=== "Podman Quadlet (root)"

    As a system service run by root, in `/etc/containers/systemd/`, use
    [`ansreview-rootful.container`](https://github.com/jwklijnsma/ansreview/blob/main/deploy/systemd/ansreview-rootful.container).
    Its header has the steps.

=== "Docker"

    ```bash
    sudo mkdir -p /etc/ansreview
    sudo install -m 600 deploy/systemd/ansreview.env.example /etc/ansreview/ansreview.env   # uncomment and set the token
    sudo cp deploy/systemd/ansreview-docker.service /etc/systemd/system/ansreview.service
    sudo systemctl edit ansreview     # REPOS=/path/to/repos, RUN_AS=uid:gid of their owner
    sudo systemctl enable --now ansreview
    ```

    It pulls the newest image on every restart
    (`sudo systemctl restart ansreview` updates it).

## Either way

- The UI is on `http://localhost:8765/?token=<your token>`.
- Logs: `journalctl [--user] -u ansreview -f`.
- The server refuses to start while the token is still the example value;
  without any token it makes up a new random one at every start.
- Both units drop all capabilities and set no-new-privileges.

## The settings file

`ansreview.env` has one `KEY=value` per line; comments only on their own line.

```bash
#ANSREVIEW_WEB_TOKEN=              # Docker service only; Podman uses the secret
#ANSREVIEW_MODEL=qwen2.5-coder:14b
#ANSREVIEW_NUM_CTX=16384
#OLLAMA_HOST=http://gpu-box.example:11434
#ANSREVIEW_LANGUAGE=en
#ANSREVIEW_FAIL_ON=high

# Review GitLab merge requests in the background
#ANSREVIEW_WATCH_MRS=1
#ANSREVIEW_GITLAB_URL=https://gitlab.example.nl
#ANSREVIEW_GITLAB_PROJECTS=platform/infra,platform/roles
#GITLAB_TOKEN=
#REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
#GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt
```

For Podman, keep the GitLab token out of this file with a secret:

```bash
printf %s 'glpat-...' | podman secret create ansreview-gitlab-token -
# and in ansreview.container:
# Secret=ansreview-gitlab-token,type=env,target=GITLAB_TOKEN
```

See [GitLab merge requests](../usage/merge-requests.md) for the watcher itself.
