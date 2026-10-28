"""The platform backend: `windows.py` or `macos.py`, whichever this machine needs.

Both modules expose the same functions, so everything above them (controller,
server, block page, appeals, usage) is platform-neutral.
"""

import sys

PLATFORM = "windows" if sys.platform == "win32" else "macos"
# Windows can type a URL into any browser's address bar, so a block doesn't need
# to know the tab's URL first; macOS needs one (except Firefox, which it types too).
CAN_REDIRECT_WITHOUT_URL = PLATFORM == "windows"

if PLATFORM == "windows":
    from .winservice import ensure_guard, install, installed, uninstall
    from .windows import (Activity, hide_app, idle_seconds, notify, observe, redirect_tab,
                          revive_ollama, screenshot)
else:
    from .macos import (Activity, hide_app, idle_seconds, notify, observe, redirect_tab,
                        revive_ollama, screenshot)
    from .service import ensure_guard, install, installed, uninstall

__all__ = [
    "PLATFORM", "CAN_REDIRECT_WITHOUT_URL", "Activity", "ensure_guard", "hide_app",
    "idle_seconds", "install", "installed", "notify", "observe", "redirect_tab",
    "revive_ollama", "screenshot", "uninstall",
]
