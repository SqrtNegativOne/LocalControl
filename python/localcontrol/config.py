import os
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(os.environ.get("LOCALCONTROL_HOME") or Path.home() / ".localcontrol")


@dataclass
class Config:
    port: int = 8765
    # auto: use whichever local server answers. ollama: /api/chat. llama: llama.app's
    # OpenAI-compatible /v1 server (a tray app that keeps the model warm).
    backend: str = "auto"
    model: str = "gemma3:4b"
    ollama_url: str = "http://127.0.0.1:11434"
    llama_url: str = "http://127.0.0.1:9931"
    # off: never screenshot. auto: screenshot only when there's no URL/title to go on.
    # always: attach a screenshot to every new judgment (slower, needs Screen Recording).
    vision: str = "auto"
    poll_seconds: float = 1.5
    idle_seconds: float = 120.0
    dry_run: bool = False
    default_minutes: float = 60.0
    max_minutes: float = 12 * 60.0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"
