"""Tests for localcontrol.server: origin/CSRF validation and live route behavior."""

import http.client
import json
import socket
import threading
import unittest

from localcontrol.config import Config
from localcontrol.controller import Policy, Verdict
from localcontrol.server import _origin_ok, make_server

PORT = 8765


class OriginOkTests(unittest.TestCase):
    def test_missing_origin_is_accepted(self):
        self.assertTrue(_origin_ok(None, PORT))

    def test_loopback_spellings_accepted(self):
        self.assertTrue(_origin_ok(f"http://127.0.0.1:{PORT}", PORT))
        self.assertTrue(_origin_ok(f"http://localhost:{PORT}", PORT))
        self.assertTrue(_origin_ok(f"http://[::1]:{PORT}", PORT))

    def test_https_loopback_accepted(self):
        self.assertTrue(_origin_ok(f"https://127.0.0.1:{PORT}", PORT))

    def test_other_hosts_rejected(self):
        self.assertFalse(_origin_ok("http://192.168.1.5:8765", PORT))
        self.assertFalse(_origin_ok("http://evil.com", PORT))
        self.assertFalse(_origin_ok("https://evil.com", PORT))

    def test_wrong_port_rejected(self):
        self.assertFalse(_origin_ok("http://127.0.0.1:8766", PORT))
        self.assertFalse(_origin_ok("http://[::1]:8766", PORT))

    def test_origins_with_paths_rejected(self):
        self.assertFalse(_origin_ok(f"http://127.0.0.1:{PORT}/", PORT))
        self.assertFalse(_origin_ok(f"http://127.0.0.1:{PORT}/index.html", PORT))

    def test_origins_with_query_or_fragment_rejected(self):
        self.assertFalse(_origin_ok(f"http://127.0.0.1:{PORT}?x=1", PORT))
        self.assertFalse(_origin_ok(f"http://127.0.0.1:{PORT}#frag", PORT))

    def test_origins_with_userinfo_rejected(self):
        self.assertFalse(_origin_ok(f"http://user@127.0.0.1:{PORT}", PORT))

    def test_non_http_scheme_rejected(self):
        self.assertFalse(_origin_ok(f"ftp://127.0.0.1:{PORT}", PORT))

    def test_malformed_port_rejected(self):
        self.assertFalse(_origin_ok("http://127.0.0.1:notaport", PORT))


class _StubHistory:
    def __init__(self):
        self.items = []

    def top(self, n=8):
        return list(self.items)

    def forget(self, text):
        self.items = [i for i in self.items if i["text"] != text]


class _StubController:
    """The slice of Controller's interface each server route touches."""

    def __init__(self, port):
        self.cfg = Config(port=port)
        self.blocks = {}
        self.history = _StubHistory()
        self.calls = []

    def snapshot(self):
        self.calls.append("snapshot")
        return {"policy": None, "model": {"ok": True}, "decisions": [], "current": None}

    def set_policy(self, text, minutes=None):
        self.calls.append(("set_policy", text, minutes))
        import time

        return Policy(command=text, rule=text, started_at=time.time(),
                      expires_at=time.time() + 60)

    def stop_policy(self):
        self.calls.append("stop_policy")

    def appeal(self, url, title, justification, block_id=None):
        self.calls.append(("appeal", url, title, justification, block_id))
        return Verdict(True, "ok", "page", "appeal")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveServerTests(unittest.TestCase):
    """Drive a real ThreadingHTTPServer bound to a free loopback port."""

    def setUp(self):
        # make_server captures ctl.cfg.port at construction, so pick a port first.
        self.ctl = _StubController(_free_port())
        self.server = make_server(self.ctl)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _request(self, method, path, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def _json_body(self, obj):
        return json.dumps(obj).encode()

    def test_get_api_state_returns_200_json(self):
        status, body = self._request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["model"]["ok"], True)
        self.assertIn("snapshot", self.ctl.calls)

    def test_post_appeal_with_localhost_origin_accepted(self):
        body = self._json_body({"url": "", "title": "HN", "justification": "need", "block_id": None})
        status, data = self._request(
            "POST", "/api/appeal",
            headers={"Content-Type": "application/json",
                     "Origin": f"http://localhost:{self.port}"},
            body=body)
        self.assertEqual(status, 200)
        parsed = json.loads(data)
        self.assertTrue(parsed["allowed"])
        self.assertEqual(parsed["source"], "appeal")
        # Route forwarded the fields to the controller.
        self.assertIn(("appeal", "", "HN", "need", None), self.ctl.calls)

    def test_post_appeal_with_evil_origin_rejected(self):
        body = self._json_body({"url": "", "title": "HN", "justification": "need"})
        status, data = self._request(
            "POST", "/api/appeal",
            headers={"Content-Type": "application/json", "Origin": "http://evil.com"},
            body=body)
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(data)["error"], "bad origin")
        self.assertNotIn("appeal", [c if isinstance(c, str) else c[0] for c in self.ctl.calls])

    def test_post_with_text_plain_origin_rejected(self):
        body = self._json_body({"url": "", "title": "HN", "justification": "need"})
        status, data = self._request(
            "POST", "/api/appeal",
            headers={"Content-Type": "text/plain",
                     "Origin": f"http://localhost:{self.port}"},
            body=body)
        self.assertEqual(status, 415)
        self.assertEqual(json.loads(data)["error"], "expected application/json")

    def test_unknown_get_path_is_404(self):
        status, data = self._request("GET", "/definitely/not/a/route")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(data)["error"], "not found")

    def test_unknown_post_path_is_404(self):
        body = self._json_body({})
        status, data = self._request(
            "POST", "/definitely/not/a/route",
            headers={"Content-Type": "application/json",
                     "Origin": f"http://localhost:{self.port}"},
            body=body)
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(data)["error"], "not found")

    def test_api_block_returns_recorded_block(self):
        self.ctl.blocks["b1"] = {"url": "https://x.com/home", "title": "Home"}
        status, body = self._request("GET", "/api/block?id=b1")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["title"], "Home")

    def test_api_block_unknown_id_returns_empty(self):
        status, body = self._request("GET", "/api/block?id=missing")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {})

    def test_post_api_command_sets_policy(self):
        body = self._json_body({"text": "only papers", "minutes": 60})
        status, data = self._request(
            "POST", "/api/command",
            headers={"Content-Type": "application/json",
                     "Origin": f"http://localhost:{self.port}"},
            body=body)
        self.assertEqual(status, 200)
        parsed = json.loads(data)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["policy"]["command"], "only papers")


if __name__ == "__main__":
    unittest.main()
