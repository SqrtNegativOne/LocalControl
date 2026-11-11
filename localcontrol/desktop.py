"""The Windows desktop backend.

`windows.py` does the observing and acting, `winservice.py` installs the
Task Scheduler watchdog. Everything above them (controller, server, block page,
appeals, usage) is platform-neutral.
"""

from .windows import (
    Activity,
    hide_app,
    idle_seconds,
    notify,
    observe,
    redirect_tab,
    revive_ollama,
    screenshot,
)
from .winservice import ensure_guard, install, installed, uninstall

PLATFORM = "windows"
# Windows can type a URL into any browser's address bar, so a block does not
# need to know the tab's URL first.
CAN_REDIRECT_WITHOUT_URL = True

__all__ = [
    "CAN_REDIRECT_WITHOUT_URL",
    "PLATFORM",
    "Activity",
    "ensure_guard",
    "hide_app",
    "idle_seconds",
    "install",
    "installed",
    "notify",
    "observe",
    "redirect_tab",
    "revive_ollama",
    "screenshot",
    "uninstall",
]
