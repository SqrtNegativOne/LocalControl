"""Local dashboard + JSON API, bound to 127.0.0.1 only."""

import json
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .controller import Controller, PolicyLocked

STATIC = Path(__file__).parent / "static"


def make_server(ctl: Controller) -> ThreadingHTTPServer:
    origin = ctl.cfg.base_url

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def do_GET(self):
            path = urlsplit(self.path).path
            if path in ("/", "/index.html"):
                self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/blocked":
                self._send(200, (STATIC / "blocked.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/block":
                block_id = parse_qs(urlsplit(self.path).query).get("id", [""])[0]
                self._json(ctl.blocks.get(block_id) or {})
            elif path == "/api/state":
                self._json(ctl.snapshot())
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            # Only accept JSON from our own pages or the CLI, so arbitrary websites
            # can't end your session with a cross-site form post.
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self._json({"error": "expected application/json"}, 415)
            if self.headers.get("Origin") not in (None, origin):
                return self._json({"error": "bad origin"}, 403)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                return self._json({"error": "bad json"}, 400)

            path = urlsplit(self.path).path
            if path == "/api/command":
                text = (body.get("text") or "").strip()
                if not text:
                    return self._json({"error": "empty command"}, 400)
                minutes = body.get("minutes")
                try:
                    p = ctl.set_policy(text, float(minutes) if minutes not in (None, "") else None)
                except PolicyLocked as e:
                    return self._json({"error": str(e)}, 409)
                self._json({"ok": True, "policy": asdict(p)})
            elif path == "/api/stop":
                try:
                    ctl.stop_policy()
                except PolicyLocked as e:
                    return self._json({"error": str(e)}, 409)
                self._json({"ok": True})
            elif path == "/api/prompts/forget":
                ctl.history.forget(body.get("text", ""))
                self._json({"ok": True})
            elif path == "/api/appeal":
                v = ctl.appeal(body.get("url", ""), body.get("title", ""), (body.get("justification") or "").strip())
                self._json(asdict(v))
            else:
                self._json({"error": "not found"}, 404)

    return ThreadingHTTPServer(("127.0.0.1", ctl.cfg.port), Handler)
