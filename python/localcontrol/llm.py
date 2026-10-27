"""Minimal Ollama client (stdlib only)."""

import base64
import json
import urllib.error
import urllib.request
from pathlib import Path


class LLMError(Exception):
    pass


class Ollama:
    def __init__(self, url: str, model: str, timeout: float = 90.0):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def _request(self, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.url + path, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise LLMError(f"Ollama HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise LLMError(f"Cannot reach Ollama at {self.url}: {e}") from e

    def chat_json(self, system: str, user: str, schema: dict, images: list[Path] = ()) -> dict:
        msg = {"role": "user", "content": user}
        if images:
            msg["images"] = [base64.b64encode(Path(p).read_bytes()).decode() for p in images]
        resp = self._request(
            "/api/chat",
            {
                "model": self.model,
                "stream": False,
                "format": schema,
                "keep_alive": "30m",
                "options": {"temperature": 0},
                "messages": [{"role": "system", "content": system}, msg],
            },
        )
        try:
            return json.loads(resp["message"]["content"])
        except (KeyError, json.JSONDecodeError) as e:
            raise LLMError(f"Unparseable model output: {resp!r:.300}") from e

    def status(self) -> tuple[bool, str]:
        """(ok, message) — whether Ollama is up and the model is pulled."""
        try:
            tags = self._request("/api/tags", timeout=3)
        except LLMError as e:
            return False, str(e)
        names = {m["name"] for m in tags.get("models", [])}
        if self.model in names or f"{self.model}:latest" in names:
            return True, f"{self.model} ready"
        return False, f"Model {self.model} not pulled — run: ollama pull {self.model}"
