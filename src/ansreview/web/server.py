"""`ansreview serve`: a small self-hosted web UI on top of the CLI.

Design notes
------------
* Standard library only (http.server), no CDN assets: it runs on an air-gapped box.
* Scans and fixes run as `python -m ansreview scan|fix` subprocesses, so the web UI gets
  exactly the CLI behaviour (cache, queue.json, reports, the dirty-tree guard) for free and
  a crash in a job cannot take the server down.
* Locked down by default: binds to 127.0.0.1, every API call needs the access token,
  POSTs need a custom header (no cross-site form posts), only repositories under the
  configured --root folders are reachable, and the Ollama host can never be set from the
  browser — it comes from ~/.ansreview.yml, the environment or the server's own flags.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import hmac
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import itertools
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .. import __version__
from ..config import load_config
from ..models import Severity

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {"/": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/app.css": ("app.css", "text/css; charset=utf-8"),
                "/favicon.svg": ("favicon.svg", "image/svg+xml")}

COOKIE = "ansreview_token"
CSRF_HEADER = "X-Ansreview"
MAX_BODY = 64 * 1024
MAX_FILE = 2 * 1024 * 1024
MAX_LOG_LINES = 20000
KEEP_JOBS = 25
REPO_MARKERS = ("ansible.cfg", "roles", "playbooks", "site.yml", "site.yaml",
                "group_vars", "host_vars", "inventory", "requirements.yml")
LINT_PROFILES = ("min", "basic", "moderate", "safety", "shared", "production")
SEVERITIES = tuple(s.label for s in Severity)

GIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/~^@{}-]{0,199}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")

SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; script-src 'self'; style-src 'self'; "
                                "img-src 'self' data:; connect-src 'self'; "
                                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------- options

def _flag(cond) -> bool:
    return cond is True or cond in ("1", "true", "on", "yes")


def _only_list(value) -> list[str]:
    if not value:
        return []
    items = value if isinstance(value, list) else str(value).replace(",", "\n").splitlines()
    out = []
    for raw in items:
        p = str(raw).strip()
        if not p:
            continue
        if (len(p) > 256 or p.startswith(("-", "/")) or "\x00" in p
                or ".." in Path(p).parts or not p.isprintable()):
            raise ApiError(400, f"invalid path filter: {p[:60]!r}")
        out.append(p)
    if len(out) > 50:
        raise ApiError(400, "too many path filters (max 50)")
    return out


def build_argv(action: str, repo: Path, opts: dict) -> list[str]:
    """Translate whitelisted UI options into CLI arguments.

    Every value goes in as `--flag=value`, so nothing the browser sends can be read as
    another option, and options the UI does not offer (host, output dir, vault password
    file, config file) cannot be set at all."""
    if action not in ("scan", "fix"):
        raise ApiError(400, "action must be scan or fix")
    argv = [action, str(repo)]
    model = (opts.get("model") or "").strip()
    if model:
        if not MODEL_RE.match(model):
            raise ApiError(400, "invalid model name")
        argv.append(f"--model={model}")
    for p in _only_list(opts.get("only")):
        argv.append(f"--only={p}")
    if _flag(opts.get("no_cache")):
        argv.append("--no-cache")

    if action == "scan":
        argv.append("--format=md,json,sarif")
        diff = (opts.get("diff") or "").strip()
        if diff:
            if not GIT_REF_RE.match(diff):
                raise ApiError(400, "invalid git ref")
            argv.append(f"--diff={diff}")
            scope = opts.get("diff_scope") or "lines"
            if scope not in ("lines", "files"):
                raise ApiError(400, "diff_scope must be lines or files")
            argv.append(f"--diff-scope={scope}")
        for key, flag in (("no_llm", "--no-llm"), ("no_lint", "--no-lint"),
                          ("no_static", "--no-static")):
            if _flag(opts.get(key)):
                argv.append(flag)
        prof = (opts.get("lint_profile") or "").strip()
        if prof:
            if prof not in LINT_PROFILES:
                raise ApiError(400, "unknown lint profile")
            argv.append(f"--lint-profile={prof}")
        sev = (opts.get("min_severity") or "").strip()
        if sev:
            if sev not in SEVERITIES:
                raise ApiError(400, "unknown severity")
            argv.append(f"--min-severity={sev}")
        lang = (opts.get("lang") or "").strip()
        if lang:
            if lang not in ("en", "nl"):
                raise ApiError(400, "lang must be en or nl")
            argv.append(f"--lang={lang}")
    else:
        for key, flag in (("dry_run", "--dry-run"), ("allow_dirty", "--allow-dirty"),
                          ("no_llm", "--no-llm"), ("no_static_fix", "--no-static-fix"),
                          ("no_lint_fix", "--no-lint-fix")):
            if _flag(opts.get(key)):
                argv.append(flag)
        sev = (opts.get("min_severity") or "").strip()
        if sev:
            if sev not in SEVERITIES:
                raise ApiError(400, "unknown severity")
            argv.append(f"--min-severity={sev}")
        conf = opts.get("min_confidence")
        if conf not in (None, ""):
            try:
                c = float(conf)
            except (TypeError, ValueError):
                raise ApiError(400, "min_confidence must be a number") from None
            if not 0.0 <= c <= 1.0:
                raise ApiError(400, "min_confidence must be between 0 and 1")
            argv.append(f"--min-confidence={c}")
    return argv


# --------------------------------------------------------------------------- jobs

@dataclass
class Job:
    id: str
    repo_id: str
    repo_name: str
    action: str
    argv: list[str]
    options: dict
    status: str = "queued"          # queued | running | done | failed | cancelled
    exit_code: int | None = None
    started: float = field(default_factory=time.time)
    ended: float | None = None
    lines: deque = field(default_factory=lambda: deque(maxlen=MAX_LOG_LINES))
    total_lines: int = 0
    proc: subprocess.Popen | None = None
    cancel_requested: bool = False

    def add_line(self, text: str):
        self.lines.append(text)
        self.total_lines += 1

    def log_since(self, since: int) -> tuple[list[str], int]:
        first = self.total_lines - len(self.lines)
        start = max(since, first) - first
        return list(itertools.islice(self.lines, start, None)), self.total_lines

    def summary(self) -> dict:
        return {"id": self.id, "repo": self.repo_id, "repo_name": self.repo_name,
                "action": self.action, "options": self.options, "status": self.status,
                "exit_code": self.exit_code, "started": self.started, "ended": self.ended,
                "seconds": round((self.ended or time.time()) - self.started, 1)}


# --------------------------------------------------------------------------- app

class WebApp:
    """Everything the HTTP handler needs; kept separate so tests can drive it directly."""

    def __init__(self, roots: list[Path], token: str | None, python: str | None = None):
        self.roots = [Path(r).expanduser().resolve() for r in roots]
        for r in self.roots:
            if not r.is_dir():
                raise ValueError(f"{r} is not a directory")
        self.token = token
        self.python = python or sys.executable
        self.lock = threading.Lock()
        self.jobs: dict[str, Job] = {}
        self._status_cache: tuple[float, dict] | None = None

    # ---- auth
    def check_token(self, candidate: str | None) -> bool:
        if self.token is None:
            return True
        return bool(candidate) and hmac.compare_digest(candidate.encode(), self.token.encode())

    # ---- repositories
    @staticmethod
    def _looks_like_repo(p: Path) -> bool:
        return (p / ".git").exists() or any((p / m).exists() for m in REPO_MARKERS)

    def list_repos(self) -> list[dict]:
        out = []
        for i, root in enumerate(self.roots):
            if self._looks_like_repo(root):
                out.append(self._repo_info(f"{i}:.", root))
                continue
            try:
                children = sorted(c for c in root.iterdir()
                                  if c.is_dir() and not c.name.startswith("."))
            except OSError:
                continue
            for c in children:
                if self._looks_like_repo(c):
                    out.append(self._repo_info(f"{i}:{c.name}", c.resolve()))
        return out

    def resolve_repo(self, repo_id: str) -> Path:
        m = re.fullmatch(r"(\d+):(.+)", repo_id or "")
        if not m or int(m.group(1)) >= len(self.roots):
            raise ApiError(404, "unknown repository")
        root = self.roots[int(m.group(1))]
        name = m.group(2)
        if name != "." and ("/" in name or "\\" in name or name.startswith(".")):
            raise ApiError(404, "unknown repository")
        path = (root / name).resolve() if name != "." else root
        try:
            path.relative_to(root)
        except ValueError:
            raise ApiError(404, "unknown repository") from None
        if not path.is_dir() or not self._looks_like_repo(path):
            raise ApiError(404, "unknown repository")
        return path

    def _git(self, path: Path, *args: str) -> str | None:
        try:
            p = subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                               text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return p.stdout.strip() if p.returncode == 0 else None

    def _repo_info(self, repo_id: str, path: Path) -> dict:
        report = path / ".ansreview" / "report.json"
        info = {"id": repo_id, "name": path.name, "path": str(path),
                "branch": self._git(path, "rev-parse", "--abbrev-ref", "HEAD"),
                "report_time": None, "counts": None, "fix_time": None, "model": None}
        if report.is_file():
            info["report_time"] = report.stat().st_mtime
            try:
                data = json.loads(report.read_text(encoding="utf-8"))
                info["counts"] = data.get("counts")
                info["model"] = data.get("model")
            except (OSError, ValueError):
                pass
        patch = path / ".ansreview" / "fix.patch"
        if patch.is_file():
            info["fix_time"] = patch.stat().st_mtime
        return info

    def repo_detail(self, repo_id: str) -> dict:
        path = self.resolve_repo(repo_id)
        info = self._repo_info(repo_id, path)
        cfg, warnings = load_config(path)
        status = self._git(path, "status", "--porcelain", "-uno", "--", ".",
                           ":(exclude).ansreview")
        info.update({
            "dirty": None if status is None else bool(status),
            "changed_files": [] if not status else [ln[3:] for ln in status.splitlines()][:50],
            "config": {"model": cfg.model, "language": cfg.language,
                       "fail_on": cfg.fail_on, "min_severity": cfg.min_severity,
                       "lint_profile": cfg.lint_profile, "workers": cfg.workers},
            "config_warnings": warnings,
            "last_commit": self._git(path, "log", "-1", "--format=%h %s"),
        })
        return info

    def report(self, repo_id: str) -> dict:
        path = self.resolve_repo(repo_id)
        f = path / ".ansreview" / "report.json"
        if not f.is_file():
            return {"report": None}
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ApiError(500, f"could not read report.json: {exc}") from None
        return {"report": data, "time": f.stat().st_mtime}

    def fix_result(self, repo_id: str) -> dict:
        path = self.resolve_repo(repo_id)
        d = path / ".ansreview"
        out = {"patch": None, "report_md": None, "time": None}
        for key, name in (("patch", "fix.patch"), ("report_md", "fix-report.md")):
            f = d / name
            if f.is_file():
                out[key] = f.read_text(encoding="utf-8", errors="replace")[:MAX_FILE]
                out["time"] = max(out["time"] or 0, f.stat().st_mtime)
        return out

    def read_file(self, repo_id: str, rel: str) -> dict:
        path = self.resolve_repo(repo_id)
        if not rel or rel.startswith("/") or "\x00" in rel:
            raise ApiError(400, "invalid path")
        target = (path / rel).resolve()
        try:
            inner = target.relative_to(path)
        except ValueError:
            raise ApiError(403, "path is outside the repository") from None
        if inner.parts and inner.parts[0] == ".git":
            raise ApiError(403, "not allowed")
        if not target.is_file():
            raise ApiError(404, "file not found")
        if target.stat().st_size > MAX_FILE:
            raise ApiError(413, "file too large to show")
        return {"path": str(inner), "content": target.read_text(encoding="utf-8",
                                                                errors="replace")}

    def queue(self, repo_id: str) -> dict | None:
        f = self.resolve_repo(repo_id) / ".ansreview" / "queue.json"
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def server_status(self) -> dict:
        now = time.time()
        if self._status_cache and now - self._status_cache[0] < 15:
            return self._status_cache[1]
        from .. import lint
        from ..ollama import OllamaClient
        cfg, _ = load_config(Path.home())
        st = {"version": __version__, "ollama_host": cfg.ollama_host, "model": cfg.model,
              "ollama": None, "model_available": None, "models": [],
              "ansible_lint": lint.version(),
              "roots": [str(r) for r in self.roots], "auth": self.token is not None}
        client = OllamaClient(cfg.ollama_host, cfg.model, api_key=cfg.api_key, timeout=5)
        try:
            st["ollama"] = client.version()
            st["models"] = client.models()[:50]
            st["model_available"] = client.has_model()
        except Exception as exc:  # noqa: BLE001 - surface any connection problem
            st["ollama_error"] = f"{exc.__class__.__name__}: {str(exc)[:200]}"
        self._status_cache = (now, st)
        return st

    # ---- jobs
    def running_job(self) -> Job | None:
        return next((j for j in self.jobs.values() if j.status in ("queued", "running")), None)

    def start_job(self, repo_id: str, action: str, opts: dict) -> Job:
        path = self.resolve_repo(repo_id)
        opts = {k: v for k, v in (opts or {}).items() if v not in (None, "", False, [])}
        argv = build_argv(action, path, opts)
        with self.lock:
            busy = self.running_job()
            if busy:
                raise ApiError(409, f"a {busy.action} of {busy.repo_name} is still running; "
                                    "wait for it or cancel it first")
            job = Job(id=secrets.token_hex(6), repo_id=repo_id, repo_name=path.name,
                      action=action, argv=argv, options=opts)
            self.jobs[job.id] = job
            for old in sorted(self.jobs.values(), key=lambda j: j.started)[:-KEEP_JOBS]:
                if old.status not in ("queued", "running"):
                    self.jobs.pop(old.id, None)
        threading.Thread(target=self._run, args=(job, path), daemon=True).start()
        return job

    def _run(self, job: Job, repo: Path):
        # `python -m` puts the working directory first on sys.path, so running from inside
        # the scanned repo would let a planted ansreview.py there replace the tool. The CLI
        # gets the repo as an absolute path; run it from our own static folder instead
        # (and set PYTHONSAFEPATH, which does the same on Python 3.11+).
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8",
                   PYTHONSAFEPATH="1")
        cmd = [self.python, "-m", "ansreview", *job.argv]
        job.add_line("$ ansreview " + " ".join(job.argv[:1] + [repo.name] + job.argv[2:]))
        try:
            job.proc = subprocess.Popen(cmd, cwd=str(STATIC_DIR), env=env,
                                        stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, encoding="utf-8", errors="replace",
                                        bufsize=1, start_new_session=True)
        except OSError as exc:
            job.add_line(f"error: could not start: {exc}")
            job.status, job.ended = "failed", time.time()
            return
        job.status = "running"
        assert job.proc.stdout is not None
        for line in job.proc.stdout:
            job.add_line(line.rstrip("\n"))
        job.exit_code = job.proc.wait()
        job.ended = time.time()
        if job.cancel_requested:
            job.status = "cancelled"
        elif job.action == "scan":
            job.status = "done" if job.exit_code in (0, 1) else "failed"
        else:
            job.status = "done" if job.exit_code == 0 else "failed"
        job.add_line(f"[exit {job.exit_code}]")

    def cancel_job(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if not job:
            raise ApiError(404, "unknown job")
        if job.proc and job.proc.poll() is None:
            job.cancel_requested = True
            try:
                if hasattr(os, "killpg"):
                    os.killpg(job.proc.pid, signal.SIGTERM)
                else:
                    job.proc.terminate()
            except ProcessLookupError:
                pass
        return job

    def job_detail(self, job_id: str, since: int = 0) -> dict:
        job = self.jobs.get(job_id)
        if not job:
            raise ApiError(404, "unknown job")
        lines, nxt = job.log_since(since)
        out = job.summary()
        out.update(lines=lines, next=nxt)
        try:
            out["queue"] = self.queue(job.repo_id)
        except ApiError:
            out["queue"] = None
        return out


# --------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = f"ansreview/{__version__}"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> WebApp:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # ---- low-level
    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data, extra: dict | None = None):
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", extra)

    def _token(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            return None
        morsel = jar.get(COOKIE)
        return morsel.value if morsel else None

    def _cookie_header(self, token: str) -> str:
        return f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict"

    def _read_body(self):
        """Read the request body up front, so an early error never leaves unread bytes
        on a keep-alive connection (they would be parsed as the next request)."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            raise ApiError(413, "request too large")
        self._raw = self.rfile.read(length) if length else b""

    def _body(self) -> dict:
        raw = getattr(self, "_raw", b"")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            raise ApiError(400, "body must be JSON") from None
        if not isinstance(data, dict):
            raise ApiError(400, "body must be a JSON object")
        return data

    def _check_post(self):
        if self.headers.get(CSRF_HEADER) != "1":
            raise ApiError(403, f"missing {CSRF_HEADER} header")
        origin = self.headers.get("Origin")
        if origin and origin != "null":
            if urlsplit(origin).netloc != self.headers.get("Host", ""):
                raise ApiError(403, "cross-origin request refused")

    # ---- dispatch
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str):
        url = urlsplit(self.path)
        path = url.path
        qs = parse_qs(url.query)
        try:
            if method == "POST":
                self._read_body()
            if method == "GET" and path in STATIC_FILES:
                return self._static(path, qs)
            if not path.startswith("/api/"):
                raise ApiError(404, "not found")
            if method == "POST":
                self._check_post()
            if path == "/api/login" and method == "POST":
                return self._login()
            if not self.app.check_token(self._token()):
                raise ApiError(401, "access token required")
            return self._api(method, path, qs)
        except ApiError as exc:
            self._json(exc.status, {"error": exc.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001 - never leak a traceback page
            self._json(500, {"error": f"{exc.__class__.__name__}: {exc}"})

    def _static(self, path: str, qs: dict):
        name, ctype = STATIC_FILES[path]
        token = (qs.get("token") or [None])[0]
        if path == "/" and token:
            # Trade the token in the URL for a cookie and drop it from the address bar.
            if not self.app.check_token(token):
                raise ApiError(401, "wrong access token")
            self._send(303, b"", "text/plain", {"Location": "/",
                                                 "Set-Cookie": self._cookie_header(token)})
            return
        body = (STATIC_DIR / name).read_bytes()
        self._send(200, body, ctype)

    def _login(self):
        token = str(self._body().get("token") or "").strip()
        if not self.app.check_token(token):
            time.sleep(0.5)
            raise ApiError(401, "wrong access token")
        self._json(200, {"ok": True}, {"Set-Cookie": self._cookie_header(token)})

    def _api(self, method: str, path: str, qs: dict):
        app = self.app
        parts = [unquote(p) for p in path[len("/api/"):].split("/") if p]
        q = lambda k, d=None: (qs.get(k) or [d])[0]  # noqa: E731

        if method == "GET" and parts == ["status"]:
            return self._json(200, app.server_status())
        if method == "GET" and parts == ["repos"]:
            return self._json(200, {"repos": app.list_repos()})
        if len(parts) >= 2 and parts[0] == "repos":
            rid = parts[1]
            rest = parts[2:]
            if method == "GET" and rest == []:
                return self._json(200, app.repo_detail(rid))
            if method == "GET" and rest == ["report"]:
                return self._json(200, app.report(rid))
            if method == "GET" and rest == ["fix"]:
                return self._json(200, app.fix_result(rid))
            if method == "GET" and rest == ["file"]:
                return self._json(200, app.read_file(rid, q("path", "")))
            if method == "POST" and rest in (["scan"], ["fix"]):
                job = app.start_job(rid, rest[0], self._body())
                return self._json(202, job.summary())
        if method == "GET" and parts == ["jobs"]:
            jobs = sorted(app.jobs.values(), key=lambda j: j.started, reverse=True)
            return self._json(200, {"jobs": [j.summary() for j in jobs]})
        if len(parts) == 2 and parts[0] == "jobs" and method == "GET":
            try:
                since = int(q("since", "0"))
            except ValueError:
                since = 0
            return self._json(200, app.job_detail(parts[1], since))
        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel" and method == "POST":
            return self._json(200, app.cancel_job(parts[1]).summary())
        raise ApiError(404, "not found")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, app: WebApp, verbose: bool = False):
        host = addr[0]
        if ":" in host:
            import socket
            self.address_family = socket.AF_INET6
        super().__init__(addr, Handler)
        self.app = app
        self.verbose = verbose


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def make_server(app: WebApp, bind: str = "127.0.0.1", port: int = 8765,
                verbose: bool = False) -> _Server:
    return _Server((bind, port), app, verbose)


def serve(roots: list[Path], bind: str = "127.0.0.1", port: int = 8765,
          token: str | None = None, no_auth: bool = False, open_browser: bool = False,
          verbose: bool = False) -> int:
    if no_auth and not is_loopback(bind):
        print("error: --no-auth is only allowed when binding to localhost", file=sys.stderr)
        return 2
    if not no_auth:
        token = token or os.environ.get("ANSREVIEW_WEB_TOKEN") or secrets.token_urlsafe(24)
    try:
        app = WebApp(roots, None if no_auth else token)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        srv = make_server(app, bind, port, verbose)
    except OSError as exc:
        print(f"error: cannot listen on {bind}:{port}: {exc}", file=sys.stderr)
        return 2
    shown = "localhost" if bind in ("127.0.0.1", "::1", "localhost") else bind
    if bind in ("0.0.0.0", "::"):
        shown = "<this-host>"
    host_part = f"[{shown}]" if ":" in shown else shown
    url = f"http://{host_part}:{srv.server_address[1]}/"
    if token and not no_auth:
        url += "?token=" + quote(token)
    repos = app.list_repos()
    print(f"ansreview {__version__} web UI", file=sys.stderr)
    for r in app.roots:
        print(f"  root: {r}", file=sys.stderr)
    print(f"  {len(repos)} repositor{'y' if len(repos) == 1 else 'ies'} found", file=sys.stderr)
    print(f"  open: {url}", file=sys.stderr)
    if not is_loopback(bind):
        print("  warning: listening on a non-local address over plain HTTP; put it behind "
              "a TLS reverse proxy or an SSH tunnel", file=sys.stderr)
    if open_browser:
        import webbrowser
        webbrowser.open(url.replace("<this-host>", "localhost"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for job in list(app.jobs.values()):
            if job.proc and job.proc.poll() is None:
                app.cancel_job(job.id)
        srv.server_close()
    return 0
