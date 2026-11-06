"""Minimal Ollama client (local or remote server)."""

from __future__ import annotations

import json
import re
import time

import requests


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, host: str, model: str, *, api_key: str = "", timeout: int = 600,
                 retries: int = 2, temperature: float = 0.1, num_ctx: int = 16384):
        self.host = host.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.retries = retries
        self.temperature = temperature
        self.num_ctx = num_ctx
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "ansreview"
        if api_key:
            self.session.headers["Authorization"] = f"Bearer {api_key}"
        self._schema_supported = True
        self.tokens_in = 0
        self.tokens_out = 0

    # -- health ----------------------------------------------------------------
    def version(self) -> str:
        r = self.session.get(f"{self.host}/api/version", timeout=15)
        r.raise_for_status()
        return r.json().get("version", "?")

    def models(self) -> list[str]:
        r = self.session.get(f"{self.host}/api/tags", timeout=15)
        r.raise_for_status()
        return [m.get("name", "") for m in r.json().get("models", [])]

    def has_model(self) -> bool:
        names = self.models()
        want = self.model if ":" in self.model else self.model + ":latest"
        return want in names or self.model in names

    # -- chat ------------------------------------------------------------------
    def chat_json(self, system: str, user: str, schema: dict) -> dict:
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "format": schema if self._schema_supported else "json",
            "options": {"temperature": self.temperature, "num_ctx": self.num_ctx},
        }
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                r = self.session.post(f"{self.host}/api/chat", json=payload, timeout=self.timeout)
                if r.status_code == 400 and self._schema_supported and "format" in r.text:
                    # Older Ollama (< 0.5) only knows format: "json"
                    self._schema_supported = False
                    payload["format"] = "json"
                    continue
                if r.status_code == 404:
                    raise OllamaError(f"model '{self.model}' not found on {self.host} "
                                      f"(run: ollama pull {self.model})")
                r.raise_for_status()
                data = r.json()
                self.tokens_in += data.get("prompt_eval_count", 0) or 0
                self.tokens_out += data.get("eval_count", 0) or 0
                content = (data.get("message") or {}).get("content", "")
                return parse_json_lenient(content)
            except OllamaError:
                raise
            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
                if attempt < self.retries:
                    time.sleep(2 * (attempt + 1))
        raise OllamaError(f"Ollama request failed after {self.retries + 1} attempts: {last_exc}")


def parse_json_lenient(text: str) -> dict:
    """Parse model output that should be JSON, tolerating code fences and chatter."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("model returned no JSON object")
        data = json.loads(text[start:end + 1])
    if isinstance(data, list):
        data = {"findings": data}
    if not isinstance(data, dict):
        raise ValueError("model returned JSON that is not an object")
    return data
