"""launchd installation with a watchdog, so the monitor is hard to switch off mid-rule.

Two LaunchAgents keep each other alive:
  com.localcontrol.agent  runs the monitor (KeepAlive: relaunched if killed)
  com.localcontrol.guard  re-creates and re-loads the agent if it's unloaded or its plist deleted
and the monitor itself re-creates/re-loads the guard. Pristine plist copies live in
~/.localcontrol/launchd/ so deleted files get restored.
"""

import os
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

from .config import DATA_DIR

AGENT = "com.localcontrol.agent"
GUARD = "com.localcontrol.guard"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
BACKUP_DIR = DATA_DIR / "launchd"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _plist_path(label: str) -> Path:
    return LAUNCH_AGENTS / f"{label}.plist"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _loaded(label: str) -> bool:
    return subprocess.run(["launchctl", "print", f"{_domain()}/{label}"], capture_output=True).returncode == 0


def _bootstrap(label: str) -> None:
    subprocess.run(["launchctl", "bootstrap", _domain(), str(_plist_path(label))], capture_output=True)


def _bootout(label: str) -> None:
    subprocess.run(["launchctl", "bootout", f"{_domain()}/{label}"], capture_output=True)


def _plists(port: int) -> dict[str, dict]:
    agent_plist, backup = _plist_path(AGENT), BACKUP_DIR / f"{AGENT}.plist"
    guard_loop = (
        f"while true; do "
        f"[ -f {shlex.quote(str(agent_plist))} ] || cp {shlex.quote(str(backup))} {shlex.quote(str(agent_plist))}; "
        f"launchctl print {_domain()}/{AGENT} >/dev/null 2>&1 || "
        f"launchctl bootstrap {_domain()} {shlex.quote(str(agent_plist))}; "
        f"sleep 3; done"
    )
    return {
        AGENT: {
            "Label": AGENT,
            "ProgramArguments": [sys.executable, "-m", "localcontrol", "--port", str(port), "serve", "--no-open"],
            "WorkingDirectory": str(PROJECT_ROOT),
            "EnvironmentVariables": {"PYTHONPATH": str(PROJECT_ROOT), "LOCALCONTROL_SERVICE": "1",
                                     "PYTHONUNBUFFERED": "1"},
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 2,
            "StandardOutPath": str(DATA_DIR / "server.log"),
            "StandardErrorPath": str(DATA_DIR / "server.log"),
        },
        GUARD: {
            "Label": GUARD,
            "ProgramArguments": ["/bin/sh", "-c", guard_loop],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 2,
        },
    }


def install(port: int) -> None:
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    for label, plist in _plists(port).items():
        data = plistlib.dumps(plist)
        (BACKUP_DIR / f"{label}.plist").write_bytes(data)
        _bootout(label)
        _plist_path(label).write_bytes(data)
    for label in (AGENT, GUARD):
        _bootstrap(label)


def installed() -> bool:
    return (BACKUP_DIR / f"{GUARD}.plist").exists()


def ensure_guard() -> None:
    """Called periodically by the monitor when running as the service."""
    if not installed():
        return
    path = _plist_path(GUARD)
    if not path.exists():
        path.write_bytes((BACKUP_DIR / f"{GUARD}.plist").read_bytes())
    if not _loaded(GUARD):
        _bootstrap(GUARD)


def uninstall() -> None:
    # Remove the backups first so neither side restores the other mid-teardown.
    for label in (GUARD, AGENT):
        (BACKUP_DIR / f"{label}.plist").unlink(missing_ok=True)
    for label in (GUARD, AGENT):
        _bootout(label)
        _plist_path(label).unlink(missing_ok=True)
