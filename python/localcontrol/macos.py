"""Observing and acting on the macOS desktop via AppleScript and command-line tools."""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

# Browsers whose active tab URL we can read/set through AppleScript, by dialect.
BROWSERS = {
    "Google Chrome": "chrome",
    "Google Chrome Canary": "chrome",
    "Chromium": "chrome",
    "Brave Browser": "chrome",
    "Microsoft Edge": "chrome",
    "Vivaldi": "chrome",
    "Arc": "chrome",
    "Safari": "safari",
    "Safari Technology Preview": "safari",
    # Firefox has no AppleScript tab API: we only get the window title (and screenshots).
    "Firefox": "firefox",
    "Firefox Developer Edition": "firefox",
}


@dataclass
class Activity:
    app: str
    window_title: str = ""
    process: str = ""  # System Events process name; can differ from the app name (e.g. "firefox")
    url: str = ""
    tab_title: str = ""

    @property
    def browser(self) -> str | None:
        return BROWSERS.get(self.app)

    @property
    def domain(self) -> str:
        host = urlsplit(self.url).hostname or ""
        return host.removeprefix("www.")

    @property
    def page(self) -> str:
        """URL without fragment, used as a cache key."""
        p = urlsplit(self.url)
        return urlunsplit((p.scheme, p.netloc, p.path, p.query, ""))

    @property
    def label(self) -> str:
        if self.browser and self.url:
            return f"{self.domain} — {self.tab_title or self.url}"
        return f"{self.app} — {self.window_title}" if self.window_title else self.app

    def to_dict(self) -> dict:
        return {"app": self.app, "window_title": self.window_title, "url": self.url,
                "tab_title": self.tab_title, "domain": self.domain, "label": self.label}


def osascript(script: str, timeout: float = 4.0) -> str:
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise OSError(r.stderr.strip())
    return r.stdout.rstrip("\n")


def _q(s: str) -> str:
    """Quote a Python string as an AppleScript string literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


FRONT_APP_SCRIPT = """
tell application "System Events"
    set p to first application process whose frontmost is true
    set procName to name of p
    set appName to displayed name of p
    set pid to unix id of p
    set winTitle to ""
    try
        set winTitle to name of front window of p
    end try
end tell
return appName & linefeed & procName & linefeed & pid & linefeed & winTitle
"""


def _tab(app: str, dialect: str) -> tuple[str, str]:
    if dialect == "chrome":
        body = "set t to active tab of front window\nreturn (URL of t) & linefeed & (title of t)"
    elif dialect == "safari":
        body = "set t to current tab of front window\nreturn (URL of t) & linefeed & (name of t)"
    else:
        return "", ""
    out = osascript(f"""
tell application {_q(app)}
    if (count of windows) = 0 then return ""
    try
        {body}
    on error
        return ""
    end try
end tell""")
    url, _, title = out.partition("\n")
    return url, title


AXURL_SRC = Path(__file__).parent / "axurl.swift"


def _axurl_binary() -> Path | None:
    """Compile the Accessibility URL reader on first use (needs Xcode command line tools)."""
    from .config import DATA_DIR

    exe = DATA_DIR / "bin" / "axurl"
    if exe.exists() and exe.stat().st_mtime >= AXURL_SRC.stat().st_mtime:
        return exe
    exe.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["swiftc", "-O", "-o", str(exe), str(AXURL_SRC)], capture_output=True, text=True)
    return exe if r.returncode == 0 else None


def _ax_url(pid: str) -> str:
    exe = _axurl_binary()
    if exe is None or not pid.isdigit():
        return ""
    try:
        r = subprocess.run([str(exe), pid], capture_output=True, text=True, timeout=3)
    except subprocess.TimeoutExpired:
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def observe() -> Activity:
    app, proc, pid, win = (osascript(FRONT_APP_SCRIPT).split("\n", 3) + ["", "", ""])[:4]
    app = app.removesuffix(".app")
    act = Activity(app=app, window_title=win, process=proc)
    dialect = act.browser
    if dialect in ("chrome", "safari"):
        try:
            act.url, act.tab_title = _tab(app, dialect)
        except OSError:
            pass  # Automation permission not granted yet, or the browser is busy.
    elif dialect == "firefox":
        act.tab_title = re.sub(r"\s+[—-]\s+Mozilla Firefox.*$", "", win)
        act.url = _ax_url(pid)
    return act


def redirect_tab(act: Activity, target: str) -> bool:
    """Send the browser's current tab to `target`, if it's still showing act.url."""
    d = act.browser
    if d == "chrome":
        tab = "active tab of front window"
    elif d == "safari":
        tab = "current tab of front window"
    elif d == "firefox":
        # No tab API; drive the address bar with keystrokes (needs Accessibility permission).
        osascript(f"""
tell application {_q(act.app)} to activate
tell application "System Events"
    keystroke "l" using command down
    delay 0.1
    keystroke {_q(target)}
    key code 36
end tell""")
        return True
    else:
        return False
    out = osascript(f"""
tell application {_q(act.app)}
    if (count of windows) = 0 then return "no"
    set t to {tab}
    if (URL of t) is {_q(act.url)} then
        set URL of t to {_q(target)}
        return "yes"
    end if
    return "no"
end tell""")
    return out == "yes"


def hide_app(act: Activity) -> None:
    proc = act.process or act.app
    osascript(f'tell application "System Events" to set visible of application process {_q(proc)} to false')


def notify(title: str, message: str) -> None:
    try:
        osascript(f"display notification {_q(message[:200])} with title {_q(title)}")
    except OSError:
        pass


def screenshot(path: Path, max_px: int = 1280) -> Path | None:
    """Capture the main display, downscaled. Returns None without Screen Recording permission."""
    path.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["screencapture", "-x", "-m", "-t", "jpg", str(path)], capture_output=True)
    if r.returncode != 0 or not path.exists() or path.stat().st_size == 0:
        return None
    subprocess.run(["sips", "-Z", str(max_px), str(path)], capture_output=True)
    return path


def idle_seconds() -> float:
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, timeout=3).stdout
        m = re.search(r'"HIDIdleTime" = (\d+)', out)
        return int(m.group(1)) / 1e9 if m else 0.0
    except (OSError, subprocess.TimeoutExpired):
        return 0.0
