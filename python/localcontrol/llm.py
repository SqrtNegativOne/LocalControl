"""Minimal clients for local model servers (stdlib only).

One interface, two backends:
  Ollama  - POST /api/chat with `format` = the JSON schema (the app's original default)
  Llama   - llama.app / llama-server: OpenAI-compatible /v1/chat/completions with
            `response_format` = {"type": "json_schema", "schema": schema}
Both return a dict matching the requested schema, and raise LLMError on any trouble.
"""

import base64
import json
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


class LLMError(Exception):
    pass


class _Server:
    """Shared HTTP plumbing."""

    label = "model server"

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
            detail = e.read().decode(errors="replace")[:300]
            raise LLMError(f"{self.label} HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise LLMError(f"Cannot reach {self.label} at {self.url}: {e}") from e

    def vision(self) -> bool | None:
        """Whether the loaded model can see images; None when the server won't say."""
        return None

    def revive(self) -> None:
        """Start the model server again if it went away (fail-closed relies on it)."""


class Ollama(_Server):
    label = "Ollama"

    def revive(self) -> None:
        from . import desktop  # platform-specific: brew services, or the tray app

        desktop.revive_ollama()

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


class Llama(_Server):
    """llama.app (and llama-server): a tray app serving the OpenAI API on 127.0.0.1.

    It runs as a router, so the model id is whatever is loaded there; we ask the
    server instead of trusting the configured name.
    """

    label = "llama.app"

    def _models(self) -> list[dict]:
        for path in ("/v1/models", "/models"):  # /models is the router's own route
            try:
                data = self._request(path, timeout=3)
            except LLMError:
                continue
            models = [m for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]
            if models:
                return models
        return []

    def model_id(self) -> str:
        models = self._models()
        if not models:
            raise LLMError(f"{self.label} is running at {self.url} but has no model loaded — "
                           f"download one in the app")
        ids = [m["id"] for m in models]
        for candidate in (self.model, f"{self.model}:latest"):
            if candidate in ids:
                return candidate
        return ids[0]

    def vision(self) -> bool | None:
        for model in self._models():
            modalities = (model.get("architecture") or {}).get("input_modalities")
            if modalities:
                return "image" in modalities
        return None

    def chat_json(self, system: str, user: str, schema: dict, images: list[Path] = ()) -> dict:
        content = user
        if images:
            content = [{"type": "text", "text": user}]
            for image in images:
                encoded = base64.b64encode(Path(image).read_bytes()).decode()
                content.append({"type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}})
        resp = self._request(
            "/v1/chat/completions",
            {
                "model": self.model_id(),
                "stream": False,
                "temperature": 0,
                # llama.cpp turns this into a grammar, so the reply is valid JSON.
                "response_format": {"type": "json_schema", "schema": schema},
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": content}],
            },
        )
        try:
            text = resp["choices"][0]["message"]["content"]
            return json.loads(text)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
            raise LLMError(f"Unparseable model output: {resp!r:.300}") from e

    def status(self) -> tuple[bool, str]:
        try:
            self._request("/health", timeout=3)
        except LLMError as e:
            return False, str(e)
        models = self._models()
        if not models:
            return False, (f"{self.label} is running at {self.url} but has no model loaded — "
                           f"download one in the app")
        return True, f"{models[0]['id']} ready ({self.label})"

    def revive(self) -> None:
        """The app is a tray process; start it if it isn't running."""
        if sys.platform == "win32":
            exe = shutil.which("llama")
        elif sys.platform == "darwin":
            exe = "/Applications/Llama.app"
        else:
            exe = None
        if not exe:
            return
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-a", exe], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.Popen([exe], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 creationflags=0x00000008 | 0x08000000)  # detached, no console
        except OSError:
            pass


def _listening(url: str, timeout: float = 0.25) -> bool:
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((parts.hostname or "127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def make(cfg) -> Ollama | Llama:
    """Pick a backend: with `auto`, whichever server answers on its default port."""
    if cfg.backend == "ollama":
        return Ollama(cfg.ollama_url, cfg.model)
    if cfg.backend == "llama":
        return Llama(cfg.llama_url, cfg.model)
    if _listening(cfg.ollama_url):
        return Ollama(cfg.ollama_url, cfg.model)
    if _listening(cfg.llama_url):
        return Llama(cfg.llama_url, cfg.model)
    return Ollama(cfg.ollama_url, cfg.model)  # nothing up: report the familiar one
