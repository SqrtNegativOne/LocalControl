import os
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(os.environ.get("LOCALCONTROL_HOME") or Path.home() / ".localcontrol")
# The port the launcher last bound, so later CLI calls find the same server even
# when 8765 was taken and a free port was chosen instead.
PORT_FILE = DATA_DIR / "port"


def atomic_write_text(path: Path, text: str) -> None:
    """Write text so a crash mid-write never truncates the real file.

    Writes to a sibling temp file, then ``os.replace`` swaps it onto the
    target (an atomic operation on Windows); a failed write removes the
    temp file and leaves the original untouched.
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


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
    # always: attach a screenshot to every new judgment (slower).
    vision: str = "auto"
    poll_seconds: float = 1.5
    idle_seconds: float = 120.0
    dry_run: bool = False
    default_minutes: float = 60.0
    max_minutes: float = 12 * 60.0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _saved_port() -> str | None:
    try:
        return PORT_FILE.read_text().strip()
    except OSError:
        return None


def default_port() -> int:
    """The port to use when none is given: env, then the launcher's saved port, else 8765."""
    for raw in (os.environ.get("LOCALCONTROL_PORT"), _saved_port()):
        if not raw:
            continue
        try:
            port = int(raw)
        except (TypeError, ValueError):
            continue
        if 1 <= port <= 65535:
            return port
    return Config.port


def save_port(port: int) -> None:
    """Remember the bound port for later CLI calls (best effort)."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_text(PORT_FILE, str(port))
    except OSError:
        pass
