"""A tiny fake Ollama server for tests (no GPU, no model needed)."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import threading


class _Handler(BaseHTTPRequestHandler):
    calls: list = []

    def log_message(self, *args):  # silence
        pass

    def _send(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/version":
            self._send(200, {"version": "0.12.0-mock"})
        elif self.path == "/api/tags":
            self._send(200, {"models": [{"name": "qwen2.5-coder:14b"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length) or b"{}")
        _Handler.calls.append(req)
        prompt = req["messages"][-1]["content"]
        if req["messages"][0]["content"].startswith("You fix Ansible code"):
            return self._fix(prompt)
        files = re.findall(r"===== FILE: (\S+) =====", prompt)
        findings = []
        if any(f.endswith("handlers/main.yml") for f in files):
            findings.append({
                "rule": "handler-never-notified", "severity": "high", "category": "reliability",
                "file": next(f for f in files if f.endswith("handlers/main.yml")), "line": 2,
                "message": "Handler 'Restart nginx' never fires: tasks notify 'restart nginx' "
                           "(handler names are case-sensitive).",
                "suggestion": "Use the same name in notify, or add listen: restart nginx.",
                "confidence": 0.9})
            findings.append({  # low confidence -> must be filtered out
                "rule": "maybe", "severity": "low", "category": "style", "file": files[0],
                "line": 1, "message": "unsure", "suggestion": "", "confidence": 0.2})
            findings.append({  # hallucinated file -> must be dropped
                "rule": "ghost", "severity": "high", "category": "security",
                "file": "roles/ghost/tasks/main.yml", "line": 3, "message": "ghost",
                "suggestion": "", "confidence": 0.9})
        content = json.dumps({"summary": f"Reviewed {len(files)} file(s).", "findings": findings})
        # wrap in a code fence on purpose to exercise the lenient parser
        self._send(200, {"message": {"role": "assistant", "content": f"```json\n{content}\n```"},
                         "prompt_eval_count": len(prompt) // 4, "eval_count": 50})


    def _fix(self, prompt: str):
        content = "\n".join(re.sub(r"^\s*\d+ \| ?", "", l)
                            for l in prompt.split("File content:\n", 1)[1].splitlines())
        results = []
        for idx, rule in re.findall(r"^\[(\d+)\] line \S+ · \w+ · llm:([\w-]+)", prompt, re.M):
            idx = int(idx)
            if rule == "handler-never-notified":
                results.append({"id": idx, "action": "fix", "reason": "add listen",
                                "edits": [{"old": "- name: Restart nginx\n  ansible.builtin.service:",
                                           "new": "- name: Restart nginx\n  listen: restart nginx\n"
                                                  "  ansible.builtin.service:"}]})
            elif rule == "unquoted-shell-var":
                results.append({"id": idx, "action": "skip",
                                "reason": "command module, no shell: false positive", "edits": []})
            elif rule == "bad-edit":
                results.append({"id": idx, "action": "fix", "reason": "x",
                                "edits": [{"old": "this text is not in the file", "new": "y"}]})
            elif rule == "rewrite-everything":
                first = content.splitlines()[0]
                results.append({"id": idx, "action": "fix", "reason": "x",
                                "edits": [{"old": content.split(first, 1)[1].strip("\n"),
                                           "new": "- name: replaced\n  ansible.builtin.ping:"}]})
            elif rule == "breaks-yaml":
                results.append({"id": idx, "action": "fix", "reason": "x",
                                "edits": [{"old": "become: true", "new": "become: [true"}]})
        body = json.dumps({"results": results})
        self._send(200, {"message": {"role": "assistant", "content": body},
                         "prompt_eval_count": 10, "eval_count": 10})


def start() -> tuple[ThreadingHTTPServer, str]:
    _Handler.calls = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


if __name__ == "__main__":
    s, url = start()
    print(url, flush=True)
    threading.Event().wait()
