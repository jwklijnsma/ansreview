"""Ansible projects that do not use roles/ or ansible.cfg: roles next to the playbooks,
single-role repositories, playbooks only."""

import io
from pathlib import Path
import zipfile

from ansreview.discover import discover, looks_like_project
from ansreview.web.server import WebApp
from ansreview.web.uploads import UploadStore

PLAY = "---\n- name: web\n  hosts: web\n  roles:\n    - docker\n"


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def units(root):
    return {(u.kind, u.name): u.files for u in discover(root, [])}


def test_roles_next_to_the_playbooks(tmp_path):
    root = write(tmp_path / "platform", {
        "web.yml": PLAY,
        "hosts.yml": "all:\n  hosts:\n    a: {}\n",
        "docker/tasks/main.yml": "- name: install\n  ansible.builtin.package: {name: docker}\n",
        "docker/handlers/main.yml": "- name: restart\n  ansible.builtin.service: {name: docker}\n",
        "docker/templates/daemon.json.j2": "{}\n",
        "loki/defaults/main.yml": "loki_port: 3100\n",
        "loki/tasks/install.yml": "- ansible.builtin.debug: {msg: hi}\n",
        "templates/motd.j2": "hello\n",
    })
    u = units(root)
    assert u[("role", "docker")] == ["docker/handlers/main.yml", "docker/tasks/main.yml",
                                     "docker/templates/daemon.json.j2"]
    # loki has no tasks/main.yml, but defaults/main.yml makes it a role; tasks/install.yml belongs to it
    assert u[("role", "loki")] == ["loki/defaults/main.yml", "loki/tasks/install.yml"]
    assert ("playbook", "web.yml") in u
    assert looks_like_project(root)


def test_single_role_repository(tmp_path):
    root = write(tmp_path / "ansible-role-ntp", {
        "tasks/main.yml": "- ansible.builtin.package: {name: chrony}\n",
        "defaults/main.yml": "ntp_servers: []\n",
        "meta/main.yml": "galaxy_info: {}\n",
        "README.md": "# ntp\n",
    })
    u = units(root)
    assert u[("role", "ansible-role-ntp")] == ["defaults/main.yml", "meta/main.yml", "tasks/main.yml"]
    assert looks_like_project(root)


def test_classic_layout_unchanged(tmp_path):
    root = write(tmp_path / "classic", {
        "site.yml": PLAY,
        "roles/docker/tasks/main.yml": "- ansible.builtin.debug: {msg: hi}\n",
        "roles/web/templates/x.j2": "x\n",          # a role with only templates still counts
        "group_vars/all.yml": "a: 1\n",
    })
    u = units(root)
    assert ("role", "docker") in u and ("role", "web") in u and ("vars", "group_vars") in u


def test_playbooks_only_and_not_ansible(tmp_path):
    assert looks_like_project(write(tmp_path / "pb", {"deploy.yml": PLAY}))
    assert not looks_like_project(write(tmp_path / "docs", {"mkdocs.yml": "site_name: x\n",
                                                            "notes.md": "x"}))
    assert not looks_like_project(write(tmp_path / "k8s", {"deploy.yaml": "apiVersion: v1\nkind: Pod\n"}))


def test_role_folders_inside_files_are_not_roles(tmp_path):
    root = write(tmp_path / "p", {
        "site.yml": PLAY,
        "roles/app/tasks/main.yml": "- ansible.builtin.debug: {msg: hi}\n",
        "roles/app/files/vendored/tasks/main.yml": "- ansible.builtin.debug: {msg: x}\n",
    })
    u = units(root)
    assert ("role", "app") in u
    assert not any(name.endswith("vendored") for _, name in u)


def test_flat_layout_zip_upload_is_accepted(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("platform/web.yml", PLAY)
        zf.writestr("platform/docker/tasks/main.yml", "- ansible.builtin.debug: {msg: hi}\n")
        zf.writestr("__MACOSX/platform/._web.yml", "junk")
        zf.writestr("platform/.git/config", "[core]\n")
    data = buf.getvalue()
    store = UploadStore(tmp_path / "up")
    meta = store.add(io.BytesIO(data), len(data), "platform.zip", WebApp._looks_like_repo)
    assert store.project(meta["id"]).name == "platform"
    assert meta["skipped"] == 2
