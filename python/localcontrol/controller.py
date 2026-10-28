"""Policy state, LLM judgments, enforcement, and usage tracking."""

import json
import os
import re
import threading
import time
import traceback
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

from . import desktop
from .desktop import Activity
from .history import PromptHistory
from .config import DATA_DIR, Config
from .llm import LLMError, make as make_llm

# Apps that are part of the OS plumbing and never worth judging.
SYSTEM_APPS = {
    # macOS
    "Finder", "loginwindow", "Dock", "SystemUIServer", "System Settings", "System Preferences",
    "Spotlight", "Control Center", "Notification Center", "NotificationCenter", "ScreenSaverEngine",
    "SecurityAgent", "UserNotificationCenter", "coreautha", "universalAccessAuthWarn",
    "Raycast", "Alfred",
    # Windows
    "explorer", "LockApp", "SearchHost", "StartMenuExperienceHost", "ShellExperienceHost",
    "ApplicationFrameHost", "TextInputHost", "Taskmgr", "conhost", "dwm", "Idle",
    "LocalControl",
}
BLANK_URL_PREFIXES = (
    "chrome://newtab", "about:blank", "about:newtab", "about:home", "edge://newtab",
    "brave://newtab", "favorites://", "arc://", "chrome-search://",
)

PARSE_SCHEMA = {
    "type": "object",
    "properties": {
        "duration_minutes": {"type": "number"},
        "rule": {"type": "string"},
    },
    "required": ["duration_minutes", "rule"],
}
PARSE_SYSTEM = """You turn a user's instruction about how their computer may be used into a structured policy.
- duration_minutes: how long the policy lasts, in minutes. Convert phrases like "for the next hour" (60), "for 90 minutes", or "until 5pm" (minutes from now to then). Use 0 if no duration or end time is given.
- rule: restate the instruction as a clear, self-contained policy describing what activity IS allowed and, if implied, what is NOT. Write it in second person ("You may only ..."). Do not mention the duration."""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {"type": "string"},
        "allowed": {"type": "boolean"},
        "scope": {"type": "string", "enum": ["site", "page"]},
    },
    "required": ["reason", "allowed", "scope"],
}
JUDGE_SYSTEM = """You are the enforcement component of a focus tool. The user set a rule for how their computer may be used right now. Given what is currently on their screen, decide whether it is consistent with the rule.

Guidelines:
- Judge by content and purpose, not just the site or app name. A search query, PDF, docs page, forum thread, or video is allowed if its subject fits the rule.
- Follow the spirit of the rule: if it permits a subject, content on that subject in any format (lecture videos, talks, slides, blog posts, tutorials) is allowed unless the rule explicitly restricts the format.
- Allow incidental infrastructure needed for allowed work: search engines when the query fits (or the search page is empty), login/SSO pages, file pickers, PDF viewers, reference managers, note-taking, cloud storage holding relevant documents.
- Block activity that clearly falls outside the rule, e.g. social media feeds, entertainment, sports, shopping, gossip, unrelated news, games, unless the rule permits them.
- If it is genuinely ambiguous, allow it.
- scope: "site" only if your verdict would hold for essentially every page of this website (or every window of this app), e.g. arxiv.org under a research-only rule; otherwise "page". Sites that host mixed content (YouTube, Reddit, Google, GitHub, Medium, X) are always "page".
- reason: one short sentence addressed to the user explaining the verdict."""


class PolicyLocked(Exception):
    pass


@dataclass
class Verdict:
    allowed: bool
    reason: str
    scope: str = "page"
    source: str = "llm"  # llm | cache | builtin | appeal | error
    ts: float = field(default_factory=time.time)


@dataclass
class Policy:
    command: str
    rule: str
    started_at: float
    expires_at: float | None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    overrides: dict[str, str] = field(default_factory=dict)  # cache key -> justification

    def remaining(self) -> float | None:
        return None if self.expires_at is None else max(0.0, self.expires_at - time.time())

    def expired(self) -> bool:
        return self.expires_at is not None and time.time() >= self.expires_at


def cache_keys(act: Activity) -> tuple[str, str]:
    """(site-level key, page-level key) for an activity."""
    if act.browser and act.url:
        return f"site:{act.domain}", f"page:{act.page}"
    if act.browser:  # Firefox: only the tab title is known
        return f"app:{act.app}|{act.tab_title}", f"win:{act.app}|{act.tab_title}"
    return f"app:{act.app}", f"win:{act.app}|{act.window_title}"


def fallback_minutes(text: str) -> float:
    t = text.lower()
    if re.search(r"\bhalf an? hour\b", t):
        return 30
    if re.search(r"\b(an|one|the next) hour\b", t):
        return 60
    m = re.search(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m)\b", t)
    if not m:
        return 0
    n = float(m.group(1))
    return n * 60 if m.group(2).startswith("h") else n


class Usage:
    """Seconds of active (non-idle) time per app and per website, per day."""

    def __init__(self):
        self.day = date.today().isoformat()
        self.apps: dict[str, float] = {}
        self.sites: dict[str, float] = {}
        self._load()

    @property
    def path(self):
        return DATA_DIR / "usage" / f"{self.day}.json"

    def _load(self):
        try:
            d = json.loads(self.path.read_text())
            self.apps, self.sites = d["apps"], d["sites"]
        except (OSError, ValueError, KeyError):
            self.apps, self.sites = {}, {}

    def add(self, act: Activity, seconds: float):
        today = date.today().isoformat()
        if today != self.day:
            self.save()
            self.day = today
            self.apps, self.sites = {}, {}
        self.apps[act.app] = self.apps.get(act.app, 0) + seconds
        if act.domain:
            self.sites[act.domain] = self.sites.get(act.domain, 0) + seconds

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"apps": self.apps, "sites": self.sites}))

    def top(self, n: int = 10) -> dict:
        def srt(d):
            return [[k, round(v)] for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:n]]
        return {"day": self.day, "apps": srt(self.apps), "sites": srt(self.sites)}


class Controller:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.llm = make_llm(cfg)
        self.vision_supported = self.llm.vision()  # None when the server won't say
        self.lock = threading.RLock()
        self.policy: Policy | None = None
        self.cache: dict[str, Verdict] = {}
        self.decisions: deque[dict] = deque(maxlen=60)
        self.current: Activity | None = None
        self.current_verdict: Verdict | None = None
        self.last_error: str | None = None
        self.usage = Usage()
        self.history = PromptHistory()
        self._notified: dict[str, float] = {}
        self.blocks: dict[str, dict] = {}  # id -> details shown on the block page
        self._last_revive = 0.0
        self._stop = threading.Event()
        self._load_policy()

    # ---------- persistence ----------

    @property
    def _state_path(self):
        return DATA_DIR / "state.json"

    def _load_policy(self):
        try:
            p = Policy(**json.loads(self._state_path.read_text())["policy"])
            self.policy = None if p.expired() else p
        except (OSError, ValueError, KeyError, TypeError):
            self.policy = None

    def _save_policy(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps({"policy": asdict(self.policy) if self.policy else None}))

    def _log(self, event: str, **kw):
        entry = {"ts": time.time(), "event": event, **kw}
        self.decisions.appendleft(entry)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(DATA_DIR / "events.jsonl", "a") as f:
            f.write(json.dumps(entry) + "\n")

    # ---------- policy commands ----------

    def _locked_message(self) -> str:
        until = datetime.fromtimestamp(self.policy.expires_at)
        return f"A rule is locked in until {until:%H:%M}: {self.policy.rule}"

    def set_policy(self, command: str, minutes: float | None = None) -> Policy:
        with self.lock:
            if self.policy and not self.policy.expired():
                raise PolicyLocked(self._locked_message())
        now = datetime.now()
        try:
            parsed = self.llm.chat_json(
                PARSE_SYSTEM,
                f"Current time: {now:%A %H:%M}.\nInstruction: {command}",
                PARSE_SCHEMA,
            )
            rule = parsed.get("rule") or command
            llm_minutes = float(parsed.get("duration_minutes") or 0)
        except LLMError as e:
            self.last_error = str(e)
            rule, llm_minutes = command, 0.0
        if minutes is None:
            minutes = llm_minutes or fallback_minutes(command)
        # Rules can't be ended early, so every rule needs a bounded duration.
        minutes = min(minutes if minutes and minutes > 0 else self.cfg.default_minutes, self.cfg.max_minutes)
        p = Policy(
            command=command,
            rule=rule,
            started_at=time.time(),
            expires_at=time.time() + minutes * 60,
        )
        with self.lock:
            if self.policy and not self.policy.expired():
                raise PolicyLocked(self._locked_message())
            self.policy = p
            self.cache.clear()
            self._save_policy()
        self._log("policy", command=command, rule=rule, minutes=minutes)
        self.history.record(command, minutes)
        return p

    def stop_policy(self):
        """User-initiated stop: refused while a rule is running."""
        with self.lock:
            if self.policy and not self.policy.expired():
                raise PolicyLocked(self._locked_message())
            self.clear_policy("stopped")

    def clear_policy(self, reason: str = "stopped"):
        with self.lock:
            if self.policy:
                self._log("policy_end", command=self.policy.command, reason=reason)
            self.policy = None
            self.cache.clear()
            self._save_policy()

    # ---------- judging ----------

    def _builtin(self, act: Activity) -> Verdict | None:
        if act.app in SYSTEM_APPS or act.app == "":
            return Verdict(True, "System app", source="builtin")
        if act.browser:
            if act.url.startswith(self.cfg.base_url):
                return Verdict(True, "LocalControl", source="builtin")
            if act.url.startswith(BLANK_URL_PREFIXES) or not (act.url or act.tab_title):
                return Verdict(True, "Blank tab", source="builtin")
        return None

    def _wants_screenshot(self, act: Activity) -> bool:
        if self.cfg.vision == "off" or self.vision_supported is False:
            return False  # this model can't see, so an image would just error out
        if self.cfg.vision == "always":
            return True
        if self.cfg.vision == "auto":
            no_url = act.browser is not None and not act.url
            no_title = act.browser is None and not act.window_title
            return no_url or no_title
        return False

    def _ask_llm(self, policy: Policy, act: Activity, justification: str = "") -> Verdict:
        lines = [f"RULE: {policy.rule}", f'(Original instruction: "{policy.command}")', "", "CURRENT ACTIVITY",
                 f"Application: {act.app}"]
        if act.url:
            lines.append(f"URL: {act.url}")
        if act.tab_title:
            lines.append(f"Page title: {act.tab_title}")
        if act.window_title and act.window_title != act.tab_title:
            lines.append(f"Window title: {act.window_title}")
        if justification:
            lines += ["", f"The user explains why they need this: {justification}",
                      "Take the explanation into account if it is plausible and consistent with the rule."]
        images = []
        if self._wants_screenshot(act):
            shot = desktop.screenshot(DATA_DIR / "screen.jpg")
            if shot:
                images.append(shot)
                lines += ["", "A screenshot of the screen is attached."]
        out = self.llm.chat_json(JUDGE_SYSTEM, "\n".join(lines), JUDGE_SCHEMA, images)
        return Verdict(bool(out.get("allowed", True)), str(out.get("reason", "")),
                       "site" if out.get("scope") == "site" else "page")

    def evaluate(self, act: Activity) -> Verdict | None:
        """Verdict for an activity under the current policy, or None if no policy is active."""
        with self.lock:
            policy = self.policy
            if policy is None:
                return None
            if (v := self._builtin(act)) is not None:
                return v
            site, page = cache_keys(act)
            for k in (page, site):
                if k in policy.overrides:
                    return Verdict(True, "Allowed by your appeal", source="appeal")
            for k in (page, site):
                if k in self.cache:
                    c = self.cache[k]
                    return Verdict(c.allowed, c.reason, c.scope, source="cache", ts=c.ts)
        # Call the model outside the lock so the UI stays responsive.
        try:
            v = self._ask_llm(policy, act)
            self.last_error = None
        except LLMError as e:
            # Fail closed: otherwise killing Ollama would be an easy way out of a rule.
            # Not cached, so things recover as soon as the model is back.
            self.last_error = str(e)
            self._revive_model()
            return Verdict(False, "The local model is unavailable, so unjudged activity is blocked until it's back.",
                           source="error")
        with self.lock:
            if self.policy is policy:
                self.cache[site if v.scope == "site" else page] = v
        self._log("judge", activity=act.to_dict(), allowed=v.allowed, reason=v.reason, scope=v.scope)
        return v

    def _revive_model(self):
        if time.time() - self._last_revive < 30:
            return
        self._last_revive = time.time()
        try:
            self.llm.revive()
        except OSError:
            pass

    def appeal(self, url: str, title: str, justification: str) -> Verdict:
        with self.lock:
            policy = self.policy
        if policy is None:
            return Verdict(True, "No active policy", source="builtin")
        act = Activity(app="Google Chrome", url=url, tab_title=title)
        try:
            v = self._ask_llm(policy, act, justification)
        except LLMError as e:
            return Verdict(False, f"Model unavailable: {e}", source="error")
        v.source = "appeal"
        if v.allowed:
            with self.lock:
                policy.overrides[cache_keys(act)[1]] = justification
                self._save_policy()
        self._log("appeal", url=url, justification=justification, allowed=v.allowed, reason=v.reason)
        return v

    # ---------- enforcement ----------

    def enforce(self, act: Activity, v: Verdict):
        if self.cfg.dry_run:
            return
        policy = self.policy
        if act.browser and (act.url or act.browser == "firefox" or desktop.CAN_REDIRECT_WITHOUT_URL):
            block_id = uuid.uuid4().hex[:8]
            self.blocks[block_id] = {"url": act.url, "title": act.tab_title, "reason": v.reason,
                                     "rule": policy.rule if policy else ""}
            while len(self.blocks) > 200:
                self.blocks.pop(next(iter(self.blocks)))
            target = f"{self.cfg.base_url}/blocked?id={block_id}"
            try:
                desktop.redirect_tab(act, target)
            except OSError as e:
                self.last_error = f"Could not redirect tab: {e}"
        else:
            try:
                desktop.hide_app(act)
            except OSError as e:
                self.last_error = f"Could not hide {act.app}: {e}"
            key = cache_keys(act)[1]
            if time.time() - self._notified.get(key, 0) > 30:
                self._notified[key] = time.time()
                desktop.notify(f"LocalControl blocked {act.app}", v.reason)

    # ---------- main loop ----------

    def run(self):
        last = time.time()
        last_save = last
        while not self._stop.is_set():
            now = time.time()
            dt, last = min(now - last, self.cfg.poll_seconds * 3), now
            try:
                self._tick(dt)
            except Exception:
                self.last_error = traceback.format_exc(limit=3)
            if now - last_save > 10:
                self._protect()
                last_save = now
            self._stop.wait(self.cfg.poll_seconds)
        self.usage.save()

    def _protect(self):
        """Periodic housekeeping that also undoes tampering: rewrite state, keep the watchdog loaded."""
        self.usage.save()
        with self.lock:
            if self.policy:
                self._save_policy()
        if os.environ.get("LOCALCONTROL_SERVICE"):
            desktop.ensure_guard()

    def _tick(self, dt: float):
        with self.lock:
            if self.policy and self.policy.expired():
                desktop.notify("LocalControl", f"Session over: {self.policy.command}")
                self.clear_policy("expired")
        try:
            act = desktop.observe()
        except Exception as e:
            self.last_error = f"Cannot read the frontmost window: {e}"
            return
        if desktop.idle_seconds() < self.cfg.idle_seconds:
            self.usage.add(act, dt)
        v = self.evaluate(act)
        with self.lock:
            self.current, self.current_verdict = act, v
        if v is not None and not v.allowed:
            self.enforce(act, v)

    def stop(self):
        self._stop.set()

    # ---------- UI state ----------

    def snapshot(self) -> dict:
        ok, status = self.llm.status()
        with self.lock:
            p = self.policy
            return {
                "policy": None if p is None else {
                    "command": p.command, "rule": p.rule, "started_at": p.started_at,
                    "expires_at": p.expires_at, "remaining": p.remaining(),
                    "overrides": list(p.overrides),
                },
                "current": None if self.current is None else {
                    **self.current.to_dict(),
                    "verdict": None if self.current_verdict is None else asdict(self.current_verdict),
                },
                "decisions": list(self.decisions)[:40],
                "usage": self.usage.top(),
                "prompts": self.history.top(),
                "model": {"name": self.cfg.model, "ok": ok, "status": status, "vision": self.cfg.vision},
                "dry_run": self.cfg.dry_run,
                "error": self.last_error,
            }
