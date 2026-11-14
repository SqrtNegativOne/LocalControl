"""Minimal clients for local model servers (stdlib only).

One interface, two backends:
  Ollama  - POST /api/chat with `format` = the JSON schema (the app's original default)
  Llama   - llama.app / llama-server: OpenAI-compatible /v1/chat/completions with
            `response_format` = {"type": "json_schema", "schema": schema}
Both return a dict matching the requested schema, and raise LLMError on any trouble.
"""

import base64
import json
import re
import shutil
import socket
import subprocess
import time
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
                raw = r.read()  # hold the body: json.load would consume it first
                try:
                    return json.loads(raw)
                except ValueError as e:  # non-JSON or empty body
                    raise LLMError(
                        f"{self.label} returned invalid JSON: {e}; body: {raw[:300]!r}"
                    ) from e
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise LLMError(f"{self.label} HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise LLMError(f"Cannot reach {self.label} at {self.url}: {e}") from e
        except OSError as e:  # reading/decoding the body failed mid-stream
            raise LLMError(f"{self.label} response read failed: {e}") from e

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
        raw = tags.get("models") if isinstance(tags, dict) else None
        names = {
            m.get("name")
            for m in (raw if isinstance(raw, list) else [])
            if isinstance(m, dict) and m.get("name")
        }
        if self.model in names or f"{self.model}:latest" in names:
            return True, f"{self.model} ready"
        return False, f"Model {self.model} not pulled — run: ollama pull {self.model}"


class Llama(_Server):
    """llama.app (and llama-server): a tray app serving the OpenAI API on 127.0.0.1.

    It runs as a router, so the model id is whatever is loaded there; we ask the
    server instead of trusting the configured name.
    """

    label = "llama.app"

    # The loaded model id rarely changes; cache it so chat_json doesn't hit
    # /v1/models on every judgment. A short TTL re-checks after a model switch.
    _model_id_ttl = 60.0

    def __init__(self, url: str, model: str, timeout: float = 90.0):
        super().__init__(url, model, timeout)
        self._model_id_cache: tuple[str, float] | None = None

    def _models(self) -> list[dict]:
        for path in ("/v1/models", "/models"):  # /models is the router's own route
            try:
                data = self._request(path, timeout=3)
            except LLMError:
                continue
            if not isinstance(data, dict):
                continue
            raw = data.get("data")
            models = [m for m in (raw if isinstance(raw, list) else [])
                      if isinstance(m, dict) and isinstance(m.get("id"), str)]
            if models:
                return models
        return []

    @staticmethod
    def _can_generate(model: dict) -> bool:
        """Whether this model produces text (some llama.app models only decide)."""
        outputs = (model.get("architecture") or {}).get("output_modalities")
        return not outputs or "text" in outputs

    _BILLIONS = re.compile(r"(\d+(?:\.\d+)?)\s*[bB](?![a-zA-Z])")

    @classmethod
    def _size(cls, model: dict) -> float:
        """Parameter count when the server reports one, else from the model id.

        The router only fills `meta` for loaded models, so unloaded entries are
        compared by names like "Qwen3-4B" / "gemma-3-1b" instead.
        """
        params = (model.get("meta") or {}).get("n_params")
        if isinstance(params, int):
            return float(params)
        sizes = [float(n) for n in cls._BILLIONS.findall(model.get("id") or "")]
        return max(sizes, default=0.0)

    def _chosen(self, models: list[dict]) -> dict:
        """The configured model if it can chat, else a loaded/largest generating one."""
        wanted = (self.model, f"{self.model}:latest")
        for name in wanted:
            for model in models:
                if model["id"] == name and self._can_generate(model):
                    return model
        generating = [m for m in models if self._can_generate(m)]
        if generating:
            loaded = [m for m in generating if (m.get("status") or {}).get("value") == "loaded"]
            # Without a configured match, prefer whatever the user loaded; otherwise
            # the largest model, so a better installed model isn't shadowed by a
            # smaller one that merely comes first in the list.
            return max(loaded or generating, key=self._size)
        for name in wanted:  # nothing declares modalities: honour the config if present
            for model in models:
                if model["id"] == name:
                    return model
        return models[0]

    def model_id(self) -> str:
        cached = self._model_id_cache
        if cached and time.time() - cached[1] < self._model_id_ttl:
            return cached[0]
        models = self._models()
        if not models:
            raise LLMError(f"{self.label} is running at {self.url} but has no model loaded - "
                           f"download one in the app")
        chosen = self._chosen(models)["id"]
        self._model_id_cache = (chosen, time.time())
        return chosen

    def vision(self) -> bool | None:
        models = self._models()
        if not models:
            return None
        modalities = (self._chosen(models).get("architecture") or {}).get("input_modalities")
        return "image" in modalities if modalities else None

    def chat_json(self, system: str, user: str, schema: dict, images: list[Path] = ()) -> dict:
        content = user
        if images:
            content = [{"type": "text", "text": user}]
            for image in images:
                encoded = base64.b64encode(Path(image).read_bytes()).decode()
                content.append({"type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}})
        body = {
            "model": self.model_id(),
            "stream": False,
            "temperature": 0,
            # OpenAI-compatible servers turn this into a grammar, so the reply is valid JSON.
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "localcontrol", "schema": schema},
            },
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": content}],
        }
        try:
            resp = self._request("/v1/chat/completions", body)
        except LLMError as e:
            # Older llama.cpp builds only understand the schema at the top level.
            if "HTTP 400" not in str(e) and "HTTP 422" not in str(e):
                raise
            body["response_format"] = {"type": "json_schema", "schema": schema}
            resp = self._request("/v1/chat/completions", body)
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
            return False, (f"{self.label} is running at {self.url} but has no model loaded - "
                           f"download one in the app")
        chosen = self._chosen(models)
        if not self._can_generate(chosen):
            return False, (f"{self.label} has no text model - load a chat model in the app "
                           f"(only {chosen['id']} is available)")
        return True, f"{chosen['id']} ready ({self.label})"

    def revive(self) -> None:
        """The app is a tray process; start it if it isn't running."""
        exe = shutil.which("llama")
        if not exe:
            return
        try:
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
