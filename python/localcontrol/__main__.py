"""LocalControl CLI.

    python -m localcontrol serve [--model gemma3:4b] [--vision auto] [--dry-run]
    python -m localcontrol cmd "for the next hour, only papers or technical material"
    python -m localcontrol history [N]  # list saved prompts / reuse prompt N
    python -m localcontrol status
    python -m localcontrol stop        # only once the rule has expired
    python -m localcontrol install     # run at login under launchd, with a watchdog
"""

import argparse
import json
import sys
import threading
import urllib.error
import urllib.request
import webbrowser

from .config import Config


def _api(port: int, path: str, body: dict | None = None) -> dict:
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(json.load(e).get("error", str(e)))
    except urllib.error.URLError as e:
        sys.exit(f"LocalControl isn't running on port {port} ({e}). Start it with: python -m localcontrol serve")


def _active_rule(port: int) -> bool:
    """Ask the running server; fall back to the saved state if it's not reachable."""
    import time

    from .config import DATA_DIR

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=10) as r:
            return json.load(r)["policy"] is not None
    except (OSError, ValueError, KeyError):
        pass
    try:
        p = json.loads((DATA_DIR / "state.json").read_text())["policy"]
        return p is not None and p["expires_at"] > time.time()
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _fmt_minutes(seconds: float | None) -> str:
    return "until stopped" if seconds is None else f"{seconds / 60:.0f} min left"


def main():
    ap = argparse.ArgumentParser(prog="localcontrol")
    ap.add_argument("--port", type=int, default=Config.port)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the monitor and dashboard")
    s.add_argument("--model", default=Config.model)
    s.add_argument("--ollama-url", default=Config.ollama_url)
    s.add_argument("--vision", choices=["off", "auto", "always"], default=Config.vision)
    s.add_argument("--poll", type=float, default=Config.poll_seconds, help="seconds between checks")
    s.add_argument("--dry-run", action="store_true", help="judge and log, but don't block anything")
    s.add_argument("--no-open", action="store_true", help="don't open the dashboard in a browser")

    c = sub.add_parser("cmd", help="set a usage rule")
    c.add_argument("text", nargs="+")
    c.add_argument("--minutes", type=float, help="override the duration parsed from the text")

    h = sub.add_parser("history", help="list saved prompts, most used first")
    h.add_argument("n", nargs="?", type=int, help="reuse prompt number N")

    sub.add_parser("install", help="run at login under launchd, with a watchdog")
    sub.add_parser("uninstall", help="remove the launchd service (refused while a rule is active)")
    sub.add_parser("status", help="show the active rule and current verdict")
    sub.add_parser("stop", help="end the active rule")

    a = ap.parse_args()

    if a.cmd == "serve":
        from .controller import Controller
        from .server import make_server

        cfg = Config(port=a.port, model=a.model, ollama_url=a.ollama_url, vision=a.vision,
                     poll_seconds=a.poll, dry_run=a.dry_run)
        ctl = Controller(cfg)
        ok, msg = ctl.llm.status()
        print(f"[localcontrol] model: {msg}")
        httpd = make_server(ctl)
        threading.Thread(target=ctl.run, daemon=True).start()
        print(f"[localcontrol] dashboard: {cfg.base_url}" + ("  (dry run)" if cfg.dry_run else ""))
        if not a.no_open:
            webbrowser.open(cfg.base_url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            ctl.stop()
    elif a.cmd == "cmd":
        r = _api(a.port, "/api/command", {"text": " ".join(a.text), "minutes": a.minutes})
        p = r["policy"]
        left = None if p["expires_at"] is None else p["expires_at"] - p["started_at"]
        print(f"Rule set ({_fmt_minutes(left)}):\n  {p['rule']}")
    elif a.cmd == "status":
        st = _api(a.port, "/api/state")
        p = st["policy"]
        print("No active rule." if p is None else f"Rule ({_fmt_minutes(p['remaining'])}): {p['rule']}")
        cur = st["current"]
        if cur:
            v = cur.get("verdict")
            mark = "-" if v is None else ("allowed" if v["allowed"] else "BLOCKED")
            print(f"Now: {cur['label']}  [{mark}{': ' + v['reason'] if v else ''}]")
        print(f"Model: {st['model']['status']}")
        if st["error"]:
            print(f"Last error: {st['error']}")
    elif a.cmd == "history":
        from .history import PromptHistory

        items = PromptHistory().top(20)
        if a.n is None:
            for i, p in enumerate(items, 1):
                print(f"{i:2d}. ({p['count']}×, {p.get('minutes', 0):.0f} min) {p['text']}")
            if not items:
                print("No saved prompts yet.")
        elif 1 <= a.n <= len(items):
            p = items[a.n - 1]
            r = _api(a.port, "/api/command", {"text": p["text"], "minutes": p.get("minutes")})
            print(f"Rule set: {r['policy']['rule']}")
        else:
            sys.exit(f"No prompt #{a.n}")
    elif a.cmd == "install":
        from . import service

        service.install(a.port)
        print(f"Installed. LocalControl now runs at login and restarts if killed. Dashboard: http://127.0.0.1:{a.port}")
    elif a.cmd == "uninstall":
        from . import service

        if _active_rule(a.port):
            sys.exit("A rule is active; uninstall is refused until it ends (see: localcontrol status).")
        service.uninstall()
        print("Uninstalled.")
    elif a.cmd == "stop":
        _api(a.port, "/api/stop", {})
        print("Rule cleared.")


if __name__ == "__main__":
    main()
