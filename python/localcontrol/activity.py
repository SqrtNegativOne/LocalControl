"""What the user is currently doing, and how to read/set a browser's current tab.

Platform-neutral on purpose: `macos.py` and `windows.py` each fill in an `Activity`,
and the rest of the app (controller, usage, verdict cache) only reads it.
"""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

# Browsers whose active tab URL we can read/set, keyed by the name macOS reports
# (the display name from System Events), and by the Windows executable name.
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

WINDOWS_BROWSERS = {
    "chrome": "chrome",
    "chrome_proxy": "chrome",
    "chromium": "chrome",
    "brave": "chrome",
    "msedge": "chrome",
    "vivaldi": "chrome",
    "opera": "chrome",
    "opera_gx": "chrome",
    "arc": "chrome",
    "thorium": "chrome",
    "yandex": "chrome",
    "firefox": "firefox",
    "firefoxdeveloperedition": "firefox",
    "librewolf": "firefox",
    "waterfox": "firefox",
    "zen": "firefox",
}

# One case-insensitive lookup: macOS names and Windows executable stems never collide.
_DIALECTS = {k.lower(): v for k, v in (BROWSERS | WINDOWS_BROWSERS).items()}

# Titles browsers append to the page title on Windows, e.g. "Hacker News - Google Chrome",
# and the "— Profile 4" Firefox adds when you use browser profiles.
_WINDOW_SUFFIX = re.compile(
    r"\s+[—–-]\s+(Mozilla Firefox|Firefox Developer Edition|Google Chrome|Chromium|Brave|"
    r"Microsoft\xa0?Edge|Vivaldi|Opera|Arc|LibreWolf|Waterfox).*$"
)
_PROFILE_SUFFIX = re.compile(r"\s+[—–-]\s+Profile \d+$", re.I)

_SCHEME_URL = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)
# Schemes that browsers show without "//" and that we must pass through untouched.
_OPAQUE_SCHEMES = (
    "about:", "chrome:", "edge:", "brave:", "vivaldi:", "view-source:", "file:", "data:",
    "moz-extension:", "chrome-extension:", "devtools:",
)
_HOSTISH = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+(:\d+)?([/?#].*)?$", re.S)
_LOCALHOST = re.compile(r"^localhost(:\d+)?([/?#].*)?$", re.I)
_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}(:\d+)?([/?#].*)?$")


def strip_window_suffix(title: str) -> str:
    """Drop the " - Google Chrome" / " — Profile 4 — Mozilla Firefox" tail from a title."""
    return _PROFILE_SUFFIX.sub("", _WINDOW_SUFFIX.sub("", title or "")).strip()


def as_url(text: str) -> str:
    """Turn an address-bar string into a URL, or "" when it's really a search query.

    Browsers show the bare host ("github.com/x") for http(s) pages, which
    urlparse() would read as a path, so give it a scheme.
    """
    text = (text or "").strip().strip("\x00")
    if not text or len(text) > 2048 or any(c.isspace() for c in text):
        return ""
    if _SCHEME_URL.match(text) or text.lower().startswith(_OPAQUE_SCHEMES):
        return text
    if _LOCALHOST.match(text) or _IPV4.match(text):
        return "http://" + text  # local dev servers: http, not https
    if _HOSTISH.match(text):
        return "https://" + text
    return ""


@dataclass
class Activity:
    app: str
    window_title: str = ""
    process: str = ""  # process/class name; can differ from the app name (e.g. "firefox", "Chrome_WidgetWin_1")
    url: str = ""
    tab_title: str = ""
    hwnd: int = 0  # Windows only: the top-level window handle
    pid: int = 0  # Windows only: owner process id

    @property
    def browser(self) -> str | None:
        return _DIALECTS.get(self.app.lower()) if self.app else None

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
