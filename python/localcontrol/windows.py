"""Observing and acting on the Windows desktop via Win32, UI Automation and MSAA.

This is the Windows twin of `macos.py`: it exposes the same six functions
(observe, redirect_tab, hide_app, notify, screenshot, idle_seconds) plus
`revive_ollama`, so `controller.py` doesn't care which platform it's on.

Reading the current tab is the one genuinely platform-specific problem:
  - Win32 gives the foreground window, its title, its process and its class.
  - Chromium browsers (Chrome, Edge, Brave, Vivaldi, ...) expose the address bar
    as a UI Automation `Edit` whose value is the URL.
  - Firefox does not expose its chrome through UIA; it exposes the *document*
    through MSAA, whose accValue is the page URL and accName the page title.
Both are read-only and need no permissions, unlike macOS Accessibility.
"""

import ctypes
import ctypes.wintypes as w
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from .activity import Activity, as_url, strip_window_suffix
from .config import DATA_DIR

HRESULT = ctypes.c_long

u32 = ctypes.WinDLL("user32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32", use_last_error=True)
oleaut32 = ctypes.WinDLL("oleaut32", use_last_error=True)
oleacc = ctypes.WinDLL("oleacc", use_last_error=True)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SW_MINIMIZE = 6
CF_UNICODETEXT = 13

# UI Automation ids (see UIAutomationClient.h).
UIA_EDIT_CONTROL_TYPE = 50004
UIA_CONTROL_TYPE_PROPERTY = 30003
UIA_VALUE_PATTERN = 10002
TREE_DESCENDANTS = 4
VT_I4 = 3
VT_DISPATCH = 9
S_OK = 0

# MSAA roles (see oleacc.h).
ROLE_SYSTEM_DOCUMENT = 0x0F
ROLE_SYSTEM_TEXT = 0x2A

# Names browsers give their address bar, in English. Localized UI falls back to
# the "does this value look like a URL" heuristic below.
ADDRESS_NAME = re.compile(r"address|location bar|search (with|or)|enter (a )?(url|address)", re.I)

# ---------- Win32 prototypes ----------

u32.GetForegroundWindow.restype = w.HWND
u32.GetWindowTextLengthW.argtypes = [w.HWND]
u32.GetWindowTextLengthW.restype = ctypes.c_int
u32.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
u32.GetWindowTextW.restype = ctypes.c_int
u32.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
u32.GetClassNameW.restype = ctypes.c_int
u32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
u32.GetWindowThreadProcessId.restype = w.DWORD
u32.GetWindow.argtypes = [w.HWND, w.UINT]
u32.GetWindow.restype = w.HWND
u32.FindWindowExW.argtypes = [w.HWND, w.HWND, w.LPCWSTR, w.LPCWSTR]
u32.FindWindowExW.restype = w.HWND
u32.EnumWindows.argtypes = [ctypes.c_void_p, w.LPARAM]
u32.EnumWindows.restype = w.BOOL
u32.IsWindow.argtypes = [w.HWND]
u32.IsWindow.restype = w.BOOL
u32.IsWindowVisible.argtypes = [w.HWND]
u32.IsWindowVisible.restype = w.BOOL
u32.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
u32.ShowWindow.restype = w.BOOL
u32.GetLastInputInfo.argtypes = [ctypes.c_void_p]
u32.GetLastInputInfo.restype = w.BOOL
u32.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
u32.GetWindowRect.restype = w.BOOL
u32.SendInput.argtypes = [w.UINT, ctypes.c_void_p, ctypes.c_int]
u32.SendInput.restype = w.UINT

k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
k32.OpenProcess.restype = w.HANDLE
k32.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
k32.QueryFullProcessImageNameW.restype = w.BOOL
k32.CloseHandle.argtypes = [w.HANDLE]
k32.GetTickCount.restype = w.DWORD
k32.GetCurrentThreadId.restype = w.DWORD

ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, w.DWORD]
ole32.CLSIDFromString.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
oleaut32.SysFreeString.argtypes = [ctypes.c_void_p]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", w.UINT), ("dwTime", w.DWORD)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class VARIANT(ctypes.Structure):
    """A 24-byte VARIANT, which is all COM needs from us on x64."""

    _fields_ = [("vt", w.USHORT), ("r1", w.USHORT), ("r2", w.USHORT), ("r3", w.USHORT),
                ("val", ctypes.c_longlong), ("pad", ctypes.c_ubyte * 8)]


def _guid(text: str) -> GUID:
    g = GUID()
    ole32.CLSIDFromString(text, ctypes.byref(g))
    return g


def _call(pv, index: int, *argtypes, ret=HRESULT):
    """Bind a vtable slot of a raw COM pointer to a callable."""
    vtbl = ctypes.cast(pv, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    return ctypes.WINFUNCTYPE(ret, ctypes.c_void_p, *argtypes)(vtbl[index])


def _release(pv) -> None:
    if pv:
        _call(pv, 2)(pv)


def _com_init() -> None:
    ole32.CoInitializeEx(None, 2)  # apartment threaded; harmless if already done


# ---------- Win32 helpers ----------


def _window_text(hwnd: int) -> str:
    n = u32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    u32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    u32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _window_pid(hwnd: int) -> int:
    pid = w.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _process_exe(pid: int) -> str:
    if not pid:
        return ""
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = w.DWORD(len(buf))
        if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value
    finally:
        k32.CloseHandle(h)


def _main_window(pid: int) -> int:
    """The first visible, titled top-level window owned by `pid`."""
    if not pid:
        return 0

    found = []

    @ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    def visit(hwnd, _lparam):
        owner = w.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and u32.IsWindowVisible(hwnd) and _window_text(hwnd):
            found.append(hwnd)
            return False
        return True

    u32.EnumWindows(ctypes.cast(visit, ctypes.c_void_p), 0)
    return found[0] if found else 0


# ---------- UI Automation (Chromium address bar) ----------


class _Automation:
    """One IUIAutomation per thread, plus a property condition matching Edits."""

    _local = threading.local()

    def __init__(self):
        _com_init()
        self.ptr = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(_guid("{FF48DBA4-60EF-4201-AA87-54103EEF594E}")),  # CLSID_CUIAutomation
            None, 1,  # CLSCTX_INPROC_SERVER
            ctypes.byref(_guid("{30CBE57D-D9D0-452A-AB13-7AC5AC4825EE}")),  # IID_IUIAutomation
            ctypes.byref(self.ptr),
        )
        if hr < 0 or not self.ptr:
            raise OSError(f"UI Automation unavailable (0x{hr & 0xFFFFFFFF:08x})")
        self.edit_condition = ctypes.c_void_p()
        variant = VARIANT(vt=VT_I4, val=UIA_EDIT_CONTROL_TYPE)
        hr = _call(self.ptr, 23, ctypes.c_int, VARIANT, ctypes.POINTER(ctypes.c_void_p))(
            self.ptr, UIA_CONTROL_TYPE_PROPERTY, variant, ctypes.byref(self.edit_condition))
        if hr < 0:
            raise OSError("UI Automation: cannot create a condition")

    @classmethod
    def get(cls):
        auto = getattr(cls._local, "auto", None)
        if auto is None:
            auto = cls._local.auto = cls()
        return auto

    def element(self, hwnd: int):
        elem = ctypes.c_void_p()
        hr = _call(self.ptr, 6, w.HWND, ctypes.POINTER(ctypes.c_void_p))(self.ptr, hwnd, ctypes.byref(elem))
        return elem if (hr >= 0 and elem) else None

    def edits(self, elem) -> list:
        """Every Edit control under `elem` (address bar first, usually)."""
        arr = ctypes.c_void_p()
        hr = _call(elem, 6, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
            elem, TREE_DESCENDANTS, self.edit_condition, ctypes.byref(arr))
        if hr < 0 or not arr:
            return []
        out = []
        try:
            n = ctypes.c_int()
            _call(arr, 3, ctypes.POINTER(ctypes.c_int))(arr, ctypes.byref(n))
            for i in range(min(n.value, 40)):
                e = ctypes.c_void_p()
                if _call(arr, 4, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
                        arr, i, ctypes.byref(e)) >= 0 and e:
                    out.append(e)
        finally:
            _release(arr)
        return out

    @staticmethod
    def name(elem) -> str:
        out = ctypes.c_wchar_p()
        if _call(elem, 23, ctypes.POINTER(ctypes.c_wchar_p))(elem, ctypes.byref(out)) < 0:
            return ""
        text = out.value or ""
        oleaut32.SysFreeString(ctypes.cast(out, ctypes.c_void_p))
        return text

    @staticmethod
    def value(elem) -> str:
        pattern = ctypes.c_void_p()
        hr = _call(elem, 16, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
            elem, UIA_VALUE_PATTERN, ctypes.byref(pattern))
        if hr < 0 or not pattern:
            return ""
        try:
            out = ctypes.c_wchar_p()
            if _call(pattern, 4, ctypes.POINTER(ctypes.c_wchar_p))(pattern, ctypes.byref(out)) < 0:
                return ""
            text = out.value or ""
            oleaut32.SysFreeString(ctypes.cast(out, ctypes.c_void_p))
            return text
        finally:
            _release(pattern)


def _uia_tab(hwnd: int) -> tuple[str, str]:
    """(url, tab title) via UI Automation, for browsers that expose the address bar."""
    try:
        auto = _Automation.get()
        root = auto.element(hwnd)
        if not root:
            return "", ""
        try:
            edits = [(auto.name(e), auto.value(e)) for e in auto.edits(root)]
        finally:
            _release(root)
    except (OSError, ctypes.ArgumentError):
        return "", ""
    for name, value in edits:
        if value and ADDRESS_NAME.search(name) and as_url(value):
            return as_url(value), ""
    for _name, value in edits:
        if value and as_url(value):
            return as_url(value), ""
    return "", ""


# ---------- MSAA (Firefox) ----------


class _Msaa:
    """A raw IAccessible pointer plus the calls we need from it."""

    def __init__(self, ptr):
        self.ptr = ptr

    @staticmethod
    def open(hwnd: int):
        _com_init()
        acc = ctypes.c_void_p()
        hr = oleacc.AccessibleObjectFromWindow(
            hwnd, 0xFFFFFFFC,  # OBJID_CLIENT
            ctypes.byref(_guid("{618736E0-3C3D-11CF-810C-00AA00389B71}")),  # IID_IAccessible
            ctypes.byref(acc))
        return _Msaa(acc) if (hr >= 0 and acc) else None

    @staticmethod
    def _string(node, index: int, child: int) -> str:
        out = ctypes.c_wchar_p()
        v = VARIANT(vt=VT_I4, val=child)
        if _call(node, index, VARIANT, ctypes.POINTER(ctypes.c_wchar_p))(node, v, ctypes.byref(out)) < 0:
            return ""
        text = out.value or ""
        oleaut32.SysFreeString(ctypes.cast(out, ctypes.c_void_p))
        return text

    def name(self, child: int, node=None) -> str:
        return self._string(node or self.ptr, 10, child)

    def value(self, child: int, node=None) -> str:
        return self._string(node or self.ptr, 11, child)

    def role(self, child: int, node=None) -> int:
        out = VARIANT()
        v = VARIANT(vt=VT_I4, val=child)
        node = node or self.ptr
        if _call(node, 13, VARIANT, ctypes.POINTER(VARIANT))(node, v, ctypes.byref(out)) < 0:
            return -1
        return int(out.val) & 0xFFFFFFFF

    def children(self, node) -> list:
        n = ctypes.c_long()
        if _call(node, 8, ctypes.POINTER(ctypes.c_long))(node, ctypes.byref(n)) < 0 or n.value <= 0:
            return []
        count = min(n.value, 400)
        arr = (VARIANT * count)()
        got = ctypes.c_long()
        if oleacc.AccessibleChildren(node, 0, count, arr, ctypes.byref(got)) < 0:
            return []
        out = []
        for i in range(got.value):
            v = arr[i]
            if v.vt == VT_DISPATCH:
                out.append((ctypes.c_void_p(v.val), 1))
            else:
                out.append((node, int(v.val) & 0xFFFFFFFF))
        return out


def _msaa_tab(hwnd: int) -> tuple[str, str]:
    """(url, tab title) from the accessible document, matched to the window title."""
    try:
        acc = _Msaa.open(hwnd)
        if acc is None:
            return "", ""
        title = _window_text(hwnd)
        queue = [(acc.ptr, 0, 0)]
        documents = []
        visited = 0
        while queue and visited < 250:
            node, child, depth = queue.pop(0)
            if depth:
                visited += 1
                if acc.role(child, node) == ROLE_SYSTEM_DOCUMENT:
                    # Only documents carry the URL; skip their (huge) subtrees.
                    url = as_url(acc.value(child, node))
                    if url:
                        documents.append((acc.name(child, node), url))
                    continue
            if depth >= 6:
                continue
            queue.extend((pv, c, depth + 1) for pv, c in acc.children(node))
    except (OSError, ctypes.ArgumentError):
        return "", ""

    # The front window's title starts with the active tab's page title.
    best = max((d for d in documents if d[0] and title.startswith(d[0])),
               key=lambda d: len(d[0]), default=None)
    if best:
        return best[1], best[0]
    if len(documents) == 1:
        return documents[0][1], documents[0][0]
    return "", ""


# ---------- tab cache ----------

_TAB_CACHE: dict[int, tuple] = {}
_TAB_LOCK = threading.Lock()


def _browser_tab(act: Activity) -> tuple[str, str]:
    """Cached (url, tab title); re-probes when the page (window title) changes."""
    now = time.time()
    with _TAB_LOCK:
        hit = _TAB_CACHE.get(act.hwnd)
    if hit and hit[0] > now and hit[1] == act.window_title:
        return hit[2], hit[3]
    if act.browser == "firefox":
        url, title = _msaa_tab(act.hwnd) or _uia_tab(act.hwnd)
    else:
        url, title = _uia_tab(act.hwnd) or _msaa_tab(act.hwnd)
    with _TAB_LOCK:
        if len(_TAB_CACHE) > 16:
            _TAB_CACHE.clear()
        _TAB_CACHE[act.hwnd] = (now + (4.0 if url else 1.0), act.window_title, url, title)
    return url, title


# ---------- the six functions ----------


def observe() -> Activity:
    hwnd = u32.GetForegroundWindow()
    if not hwnd:
        raise OSError("No foreground window")
    pid = _window_pid(hwnd)
    exe = _process_exe(pid)
    cls = _class_name(hwnd)
    title = _window_text(hwnd)
    if cls == "ApplicationFrameWindow":  # a UWP app hosted by the shell
        child = u32.FindWindowExW(hwnd, None, "Windows.UI.Core.CoreWindow", None)
        if child:
            pid = _window_pid(child)
            exe = _process_exe(pid)
    if pid == os.getpid():
        app = "LocalControl"
    else:
        app = Path(exe).stem if exe else (title or cls)
    act = Activity(app=app, window_title=title, process=cls, hwnd=hwnd, pid=pid)
    if act.browser:
        act.url, act.tab_title = _browser_tab(act)
        act.tab_title = act.tab_title or strip_window_suffix(title)
    return act


class _KeyInput(ctypes.Structure):
    _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class _InputUnion(ctypes.Union):
    _fields_ = [("ki", _KeyInput), ("pad", ctypes.c_byte * 32)]


class _Input(ctypes.Structure):
    _fields_ = [("type", w.DWORD), ("u", _InputUnion)]


KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_CONTROL, VK_L, VK_RETURN = 0x11, 0x4C, 0x0D
assert ctypes.sizeof(_Input) == 40, "KEYBDINPUT layout is wrong for this architecture"


def _send(events: list[tuple[int, int, int]]) -> None:
    """events: (virtual key, scan code, flags)."""
    array = (_Input * len(events))()
    for i, (vk, scan, flags) in enumerate(events):
        array[i].type = 1  # INPUT_KEYBOARD
        array[i].u.ki = _KeyInput(wVk=vk, wScan=scan, dwFlags=flags)
    u32.SendInput(len(events), ctypes.byref(array), ctypes.sizeof(_Input))


def _type_unicode(text: str) -> None:
    events = []
    for ch in text:
        events.append((0, ord(ch), KEYEVENTF_UNICODE))
        events.append((0, ord(ch), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    _send(events)


def redirect_tab(act: Activity, target: str) -> bool:
    """Type `target` into the address bar of the window we last saw.

    Deliberately a no-op unless that window is still in the foreground: we will
    not send keystrokes into whatever the user switched to.
    """
    hwnd = act.hwnd or _main_window(act.pid)
    if not hwnd or not u32.IsWindow(hwnd) or u32.GetForegroundWindow() != hwnd:
        return False
    _send([(VK_CONTROL, 0, 0), (VK_L, 0, 0), (VK_L, 0, KEYEVENTF_KEYUP), (VK_CONTROL, 0, KEYEVENTF_KEYUP)])
    time.sleep(0.05)
    _type_unicode(target)
    time.sleep(0.05)
    _send([(VK_RETURN, 0, 0), (VK_RETURN, 0, KEYEVENTF_KEYUP)])
    with _TAB_LOCK:
        _TAB_CACHE.pop(hwnd, None)  # the page is about to change
    return True


def hide_app(act: Activity) -> None:
    hwnd = act.hwnd or _main_window(act.pid)
    if not hwnd or not u32.IsWindow(hwnd):
        raise OSError(f"no window for {act.app}")
    u32.ShowWindow(hwnd, SW_MINIMIZE)


_TOAST = r"""
import sys, tkinter as tk
title, message = sys.argv[1], sys.argv[2]
root = tk.Tk()
root.overrideredirect(True)
root.attributes("-topmost", True)
frame = tk.Frame(root, bg="#101010", padx=14, pady=10)
frame.pack()
tk.Label(frame, text=title, fg="#ffffff", bg="#101010", font=("Segoe UI", 10, "bold"),
         anchor="w", justify="left", wraplength=320).pack(fill="x")
tk.Label(frame, text=message, fg="#b0b0b0", bg="#101010", font=("Segoe UI", 9),
         anchor="w", justify="left", wraplength=320).pack(fill="x")
root.update_idletasks()
x = root.winfo_screenwidth() - root.winfo_width() - 24
y = root.winfo_screenheight() - root.winfo_height() - 72
root.geometry(f"+{x}+{y}")
root.bind("<Button-1>", lambda _e: root.destroy())
root.after(7000, root.destroy)
root.mainloop()
"""


def notify(title: str, message: str) -> None:
    """A small always-on-top toast in its own process (tkinter needs its own main thread)."""
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    try:
        subprocess.Popen(
            [str(pythonw if pythonw.exists() else exe), "-c", _TOAST, title[:80], message[:200]],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x00000008 | 0x08000000,  # DETACHED_PROCESS | CREATE_NO_WINDOW
        )
    except OSError:
        pass


_PS_SHOT = (
    "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
    "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
    "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
    "$g=[System.Drawing.Graphics]::FromImage($bmp);"
    "$g.CopyFromScreen($b.X,$b.Y,0,0,$bmp.Size);"
    "$w=[math]::Min({max_px},$bmp.Width);$h=[int]($bmp.Height*$w/$bmp.Width);"
    "$small=New-Object System.Drawing.Bitmap $w,$h;"
    "$gs=[System.Drawing.Graphics]::FromImage($small);"
    "$gs.InterpolationMode='HighQualityBicubic';$gs.DrawImage($bmp,0,0,$w,$h);"
    "$small.Save('{path}',[System.Drawing.Imaging.ImageFormat]::Jpeg)"
)


def _screenshot_powershell(path: Path, max_px: int) -> Path | None:
    script = _PS_SHOT.format(max_px=max_px, path=str(path).replace("'", "''"))
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return path if r.returncode == 0 and path.exists() and path.stat().st_size else None


def screenshot(path: Path, max_px: int = 1280) -> Path | None:
    """Capture the screen, downscaled to `max_px` wide, as JPEG."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import ImageGrab  # optional: nicer and ~10x faster than PowerShell

        img = ImageGrab.grab(all_screens=False)
        if img.width > max_px:
            img = img.resize((max_px, max(1, round(img.height * max_px / img.width))))
        img.convert("RGB").save(path, "JPEG", quality=70)
        return path if path.exists() and path.stat().st_size else None
    except (ImportError, OSError, ValueError):
        return _screenshot_powershell(path, max_px)


def idle_seconds() -> float:
    lii = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
    if not u32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    now = k32.GetTickCount() & 0xFFFFFFFF
    return ((now - lii.dwTime) & 0xFFFFFFFF) / 1000.0


def revive_ollama() -> None:
    """Start Ollama again if the model server went away (fail-closed relies on it)."""
    app = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama app.exe"
    cmd = [str(app)] if app.exists() else ["ollama", "serve"]
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=0x00000008 | 0x08000000)


def probe() -> None:
    """`python -m localcontrol.windows` — what LocalControl sees on this machine."""
    started = time.perf_counter()
    act = observe()
    elapsed = (time.perf_counter() - started) * 1000
    first = (act.url, act.tab_title)
    cached = (time.perf_counter() - started) * 1000
    print(f"app         {act.app!r}  (pid {act.pid}, class {act.process!r})")
    print(f"window      {act.window_title!r}")
    print(f"browser     {act.browser!r}")
    print(f"url         {act.url!r}")
    print(f"tab title   {act.tab_title!r}")
    print(f"label       {act.label!r}")
    print(f"idle        {idle_seconds():.1f}s")
    print(f"observe     {elapsed:.0f} ms, {cached:.0f} ms after tab re-read")
    shot = screenshot(DATA_DIR / "probe.jpg")
    print(f"screenshot  {shot} ({shot.stat().st_size // 1024 if shot else 0} KiB)")
    print(f"re-read     {first}")


if __name__ == "__main__":
    probe()
