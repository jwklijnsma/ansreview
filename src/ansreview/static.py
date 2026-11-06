"""Deterministic, Ansible-aware checks that ansible-lint does not cover (well).

Rule IDs are AR0xx. Every check is cheap and runs without a model, so these
findings are reliable and make good context for the LLM pass.
"""

from __future__ import annotations

from pathlib import Path
import re
import shlex

import yaml

from .models import Finding, Severity

SECRET_KEY_RE = re.compile(
    r"(^|_|-)(pass(word|wd|phrase)?|pwd|secret|token|api_?key|apikey|private_?key|"
    r"client_secret|credentials?|auth_key|access_key|secret_key)($|_|-)", re.I)
NOT_SECRET_SUFFIX_RE = re.compile(
    r"(_file|_path|_dir|_length|_len|_policy|_enabled|_min|_max|_age|_expire|_expiry|"
    r"_hash_algo|_algorithm|_url|_name|_user|_username|_id|_field|_regex|_prompt|_lookup|"
    r"_command|_cmd|_env|_var|_vars|_set|_mode|_type|_format|_owner|_group)$", re.I)
PLACEHOLDER_RE = re.compile(r"^(<.*>|\*+|x+|todo|tbd|none|null|n/?a|undefined|"
                            r"vault_\w+|lookup\(.*\)|.*\{\{.*\}\}.*|\$\{?\w+\}?)$", re.I)
CURL_PIPE_RE = re.compile(r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z|da)?sh\b", re.I)
URL_CREDS_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@{}]+:[^/\s@{}]+@", re.I)
PRIVATE_KEY_RE = re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----")
WORLD_WRITABLE_RE = re.compile(r"^0?[0-7]?[0-7][0-7][2367]$|o\+w|a\+w|o=rwx|a=rwx")
SSHD_WEAK_RE = re.compile(r"^\s*(PermitRootLogin\s+yes|PasswordAuthentication\s+yes|"
                          r"PermitEmptyPasswords\s+yes)\b", re.I)

SHELL_MODULES = {"shell", "command", "raw", "ansible.builtin.shell", "ansible.builtin.command",
                 "ansible.builtin.raw", "ansible.legacy.shell", "ansible.legacy.command"}
DOWNLOAD_MODULES = {"get_url", "uri", "ansible.builtin.get_url", "ansible.builtin.uri",
                    "yum_repository", "ansible.builtin.yum_repository",
                    "apt_repository", "ansible.builtin.apt_repository",
                    "rpm_key", "ansible.builtin.rpm_key", "apt_key", "ansible.builtin.apt_key"}
FILE_MODULES = {"file", "copy", "template", "get_url", "unarchive", "ansible.builtin.file",
                "ansible.builtin.copy", "ansible.builtin.template", "ansible.builtin.get_url",
                "ansible.builtin.unarchive", "ansible.posix.acl"}
SELINUX_MODULES = {"selinux", "ansible.posix.selinux"}
SERVICE_MODULES = {"service", "systemd", "systemd_service", "ansible.builtin.service",
                   "ansible.builtin.systemd", "ansible.builtin.systemd_service"}
LINEINFILE_MODULES = {"lineinfile", "ansible.builtin.lineinfile", "blockinfile",
                      "ansible.builtin.blockinfile"}
DEBUG_MODULES = {"debug", "ansible.builtin.debug"}
TASK_KEYWORDS = {"name", "when", "loop", "with_items", "register", "become", "become_user",
                 "tags", "notify", "vars", "no_log", "ignore_errors", "failed_when",
                 "changed_when", "delegate_to", "environment", "until", "retries", "delay",
                 "block", "rescue", "always", "args", "check_mode", "run_once", "loop_control",
                 "listen", "throttle", "any_errors_fatal", "module_defaults", "collections",
                 "timeout", "diff", "async", "poll", "local_action", "action"}
FIREWALL_SERVICES = {"firewalld", "iptables", "nftables", "ufw", "auditd", "fapolicyd"}


class _VaultValue(str):
    """Marker for !vault encrypted scalars (never a plaintext secret)."""


class LineLoader(yaml.SafeLoader):
    """SafeLoader that remembers line numbers of mappings and their keys."""


def _end_line(mark) -> int:
    """1-based last line of a node. PyYAML's end mark sits where the *next* token starts,
    which for block mappings is usually the indentation (or '- ') of the following item."""
    buf = getattr(mark, "buffer", None)
    if isinstance(buf, str) and mark.pointer is not None:
        start = buf.rfind("\n", 0, mark.pointer) + 1
        if buf[start:mark.pointer].strip() in ("", "-"):
            return max(1, mark.line)       # node ended on the previous line
    return mark.line + 1


def _construct_mapping(loader, node, deep=False):
    mapping = {}
    key_lines = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            hash(key)
        except TypeError:
            key = str(key)
        mapping[key] = loader.construct_object(value_node, deep=deep)
        key_lines[key] = key_node.start_mark.line + 1
    mapping["__line__"] = node.start_mark.line + 1
    mapping["__endline__"] = _end_line(node.end_mark)
    mapping["__keylines__"] = key_lines
    return mapping


LineLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)
LineLoader.add_constructor("!vault", lambda l, n: _VaultValue(l.construct_scalar(n)))
LineLoader.add_constructor("!unsafe", lambda l, n: l.construct_scalar(n))
LineLoader.add_multi_constructor("!", lambda l, suffix, n: (
    l.construct_scalar(n) if isinstance(n, yaml.ScalarNode) else None))


def _mask(value: str) -> str:
    value = str(value)
    return (value[:2] + "***") if len(value) > 4 else "***"


def _module_of(task: dict) -> tuple[str | None, object]:
    for key, val in task.items():
        if not isinstance(key, str) or key.startswith("__"):
            continue
        if key in TASK_KEYWORDS or key.startswith("with_"):
            continue
        return key, val
    if "action" in task:
        val = task["action"]
        if isinstance(val, str):
            parts = val.split(None, 1)
            return parts[0], parts[1] if len(parts) > 1 else ""
        if isinstance(val, dict) and "module" in val:
            return val["module"], val
    return None, None


def _free_form_args(args: object) -> dict:
    """Parse 'k=v k2=v2' free-form module arguments."""
    if isinstance(args, dict):
        return args
    if not isinstance(args, str):
        return {}
    out = {}
    try:
        tokens = shlex.split(args)
    except ValueError:
        tokens = args.split()
    for tok in tokens:
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out


def _truthy(value) -> bool:
    return str(value).strip().lower() in {"true", "yes", "1", "on"}


def _falsy(value) -> bool:
    return str(value).strip().lower() in {"false", "no", "0", "off"}


def _is_secret_key(key: str) -> bool:
    return bool(SECRET_KEY_RE.search(key)) and not NOT_SECRET_SUFFIX_RE.search(key)


def _is_plaintext_secret(value) -> bool:
    if isinstance(value, _VaultValue) or not isinstance(value, str):
        return False
    v = value.strip()
    if not v or v.startswith("$ANSIBLE_VAULT"):
        return False
    return not PLACEHOLDER_RE.match(v)


class StaticChecker:
    def __init__(self, root: Path, disabled: list[str] | None = None):
        self.root = root
        self.disabled = set(disabled or [])
        self.findings: list[Finding] = []
        self.errors: list[str] = []
        self._end: int | None = None

    # -- helpers -----------------------------------------------------------
    def add(self, rule, sev, file, line, msg, category="security", suggestion="", end=None):
        if rule in self.disabled:
            return
        self.findings.append(Finding(rule, sev, file, line, msg, "static", category, suggestion,
                                     end_line=end or self._end))

    # -- entry points --------------------------------------------------------
    def check_files(self, files: list[str]) -> list[Finding]:
        for rel in files:
            path = self.root / rel
            try:
                text = path.read_text(errors="replace")
            except OSError as exc:
                self.errors.append(f"{rel}: {exc}")
                continue
            self._check_text(rel, text)
            if path.suffix.lower() in {".yml", ".yaml"} or rel.split("/")[-1] in {"all", "main"}:
                self._check_yaml(rel, text)
        return self.findings

    # -- plain-text checks (any file, including .j2 templates) ---------------
    def _check_text(self, rel: str, text: str):
        self._end = None
        for no, line in enumerate(text.splitlines(), 1):
            if PRIVATE_KEY_RE.search(line):
                self.add("AR016", Severity.CRITICAL, rel, no,
                         "Private key committed in plaintext.",
                         suggestion="Remove it, rotate the key, and store it with ansible-vault "
                                    "or an external secret store.")
            if URL_CREDS_RE.search(line) and "{{" not in line.split("@")[0]:
                self.add("AR014", Severity.HIGH, rel, no,
                         "URL contains embedded credentials (user:password@host).",
                         suggestion="Pass credentials via url_username/url_password with a "
                                    "vaulted variable, and set no_log: true.")

    # -- YAML structural checks ---------------------------------------------
    def _check_yaml(self, rel: str, text: str):
        try:
            docs = list(yaml.load_all(text, Loader=LineLoader))
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            self.add("AR000", Severity.HIGH, rel, mark.line + 1 if mark else None,
                     f"YAML parse error: {getattr(exc, 'problem', exc)}", "reliability")
            return
        for doc in docs:
            self._walk(rel, doc, parent_key=None)

    def _walk(self, rel: str, node, parent_key):
        if isinstance(node, list):
            for item in node:
                self._walk(rel, item, parent_key)
        elif isinstance(node, dict):
            self._check_mapping(rel, node)
            for k, v in node.items():
                if isinstance(k, str) and k.startswith("__"):
                    continue
                self._walk(rel, v, k)

    def _check_mapping(self, rel: str, m: dict):
        keylines = m.get("__keylines__", {})
        self._end = None
        # AR001: hard-coded secrets in vars / defaults / module args
        for key, value in m.items():
            if not isinstance(key, str) or key.startswith("__"):
                continue
            if _is_secret_key(key) and _is_plaintext_secret(value):
                self.add("AR001", Severity.HIGH, rel, keylines.get(key, m.get("__line__")),
                         f"Hard-coded secret in '{key}' (value starts '{_mask(value)}').",
                         suggestion="Move it to an ansible-vault encrypted vars file or use "
                                    "a lookup (e.g. hashi_vault) and reference it as a variable.")

        module, raw_args = _module_of(m)
        if not module or ("name" not in m and module not in SHELL_MODULES | FILE_MODULES
                          | DOWNLOAD_MODULES | SERVICE_MODULES | SELINUX_MODULES
                          | LINEINFILE_MODULES | DEBUG_MODULES):
            return
        args = _free_form_args(raw_args)
        if isinstance(m.get("args"), dict):
            args = {**args, **m["args"]}
        line = m.get("__line__")
        self._end = m.get("__endline__")
        short = module.split(".")[-1]

        # AR002: secret-looking args without no_log
        secret_args = [k for k, v in args.items() if isinstance(k, str)
                       and not k.startswith("__") and _is_secret_key(k) and v not in (None, "")]
        if secret_args and not _truthy(m.get("no_log", False)):
            self.add("AR002", Severity.MEDIUM, rel, line,
                     f"Task passes secret argument(s) {', '.join(secret_args)} to '{short}' "
                     "without no_log: true; the value can end up in logs and AWX/AAP job output.",
                     suggestion="Add `no_log: true` to the task.")

        # AR003: TLS verification disabled
        for k in ("validate_certs", "verify_ssl", "sslverify", "validate_cert"):
            if k in args and _falsy(args[k]):
                self.add("AR003", Severity.MEDIUM, rel, line,
                         f"TLS certificate validation disabled ({k}: {args[k]}) in '{short}'.",
                         suggestion="Trust the internal CA instead (e.g. ca_path / update-ca-trust).")

        # AR005: curl | sh
        if module in SHELL_MODULES:
            cmd = raw_args if isinstance(raw_args, str) else (
                args.get("cmd") or args.get("_raw_params") or "")
            if isinstance(cmd, str) and CURL_PIPE_RE.search(cmd):
                self.add("AR005", Severity.HIGH, rel, line,
                         "Remote script piped straight into a shell (curl|sh).",
                         suggestion="Download with get_url + checksum, review, then execute.")
            if isinstance(cmd, str) and re.search(r"(^|[;&|]\s*)sudo\s", cmd):
                self.add("AR013", Severity.LOW, rel, line,
                         "sudo used inside a shell/command task.", "style",
                         suggestion="Use `become: true` instead of calling sudo yourself.")

        # AR006: world-writable permissions
        if module in FILE_MODULES:
            for k in ("mode", "directory_mode", "permissions"):
                if k in args and WORLD_WRITABLE_RE.search(str(args[k]).strip("'\"")):
                    self.add("AR006", Severity.HIGH, rel, line,
                             f"World-writable permissions ({k}: {args[k]}).",
                             suggestion="Use the least permissive mode, e.g. '0644' or '0750'.")

        # AR007: downloads / repos over plain http
        if module in DOWNLOAD_MODULES:
            for k in ("url", "baseurl", "repo", "key", "mirrorlist", "gpgkey"):
                v = args.get(k)
                if isinstance(v, str) and v.strip().lower().startswith("http://"):
                    self.add("AR007", Severity.MEDIUM, rel, line,
                             f"'{short}' fetches over plain HTTP ({k}).",
                             suggestion="Use https and, for get_url, add a checksum.")
            if short == "get_url" and "checksum" not in args:
                self.add("AR009", Severity.LOW, rel, line,
                         "get_url without checksum; download integrity is not verified.",
                         "reliability", suggestion="Add `checksum: sha256:<hash>`.")
            if short in {"yum_repository"} and _falsy(args.get("gpgcheck", True)):
                self.add("AR017", Severity.MEDIUM, rel, line,
                         "Repository configured with gpgcheck disabled.",
                         suggestion="Enable gpgcheck and import the vendor GPG key.")

        # AR008: ignore_errors without a failed_when
        if _truthy(m.get("ignore_errors", False)) and "failed_when" not in m:
            self.add("AR008", Severity.LOW, rel, line,
                     "ignore_errors: true hides every failure of this task.", "reliability",
                     suggestion="Use failed_when with the exact condition you want to tolerate.")

        # AR011: weakening host security controls
        if module in SELINUX_MODULES and str(args.get("state", "")).lower() in {"disabled", "permissive"}:
            self.add("AR011", Severity.MEDIUM, rel, line,
                     f"SELinux set to {args.get('state')}.",
                     suggestion="Keep enforcing; fix policy with seboolean/sefcontext/semanage.")
        if module in SERVICE_MODULES:
            svc = str(args.get("name", "")).replace(".service", "")
            if svc in FIREWALL_SERVICES and (str(args.get("state", "")).lower() == "stopped"
                                             or _falsy(args.get("enabled", True))):
                self.add("AR011", Severity.MEDIUM, rel, line,
                         f"Security service '{svc}' is stopped or disabled.",
                         suggestion="Open the needed ports/rules instead of disabling the service.")

        # AR012: weak sshd settings written by lineinfile/blockinfile
        if module in LINEINFILE_MODULES:
            for k in ("line", "block"):
                v = args.get(k)
                if isinstance(v, str) and any(SSHD_WEAK_RE.search(x) for x in v.splitlines()):
                    self.add("AR012", Severity.MEDIUM, rel, line,
                             "Weak sshd setting (root login / password auth enabled).",
                             suggestion="Use key-based auth; PermitRootLogin prohibit-password or no.")

        # AR015: debug printing secrets
        if module in DEBUG_MODULES:
            target = str(args.get("var", "")) + " " + str(args.get("msg", ""))
            if re.search(r"(pass(word)?|secret|token|api_?key)", target, re.I):
                self.add("AR015", Severity.MEDIUM, rel, line,
                         "debug prints a secret-looking variable to the job output.",
                         suggestion="Remove the debug task or guard it with no_log / verbosity.")


RULE_DOCS = {
    "AR000": "YAML parse error",
    "AR001": "Hard-coded secret",
    "AR002": "Secret argument without no_log",
    "AR003": "TLS validation disabled",
    "AR005": "curl | sh",
    "AR006": "World-writable permissions",
    "AR007": "Plain HTTP download or repository",
    "AR008": "ignore_errors without failed_when",
    "AR009": "get_url without checksum",
    "AR011": "SELinux / firewall / audit weakened",
    "AR012": "Weak sshd configuration",
    "AR013": "sudo inside shell/command",
    "AR014": "Credentials embedded in URL",
    "AR015": "debug prints a secret",
    "AR016": "Private key committed",
    "AR017": "Repository gpgcheck disabled",
}


def run_static(root: Path, files: list[str], disabled: list[str] | None = None):
    checker = StaticChecker(root, disabled)
    checker.check_files(files)
    return checker.findings, checker.errors
