"""Tests for the pure helpers in localcontrol.activity."""

import unittest

from localcontrol.activity import Activity, as_url, strip_window_suffix


class AsUrlTests(unittest.TestCase):
    def test_bare_host_gets_https(self):
        self.assertEqual(as_url("github.com/x"), "https://github.com/x")
        self.assertEqual(as_url("example.com"), "https://example.com")
        self.assertEqual(as_url("sub.domain.example.co.uk/path?q=1#f"),
                         "https://sub.domain.example.co.uk/path?q=1#f")

    def test_localhost_gets_http(self):
        self.assertEqual(as_url("localhost"), "http://localhost")
        self.assertEqual(as_url("localhost:3000"), "http://localhost:3000")

    def test_ipv4_gets_http(self):
        self.assertEqual(as_url("127.0.0.1:8765"), "http://127.0.0.1:8765")
        self.assertEqual(as_url("192.168.1.1/admin"), "http://192.168.1.1/admin")

    def test_opaque_schemes_pass_through(self):
        self.assertEqual(as_url("about:blank"), "about:blank")
        self.assertEqual(as_url("file:///C:/x"), "file:///C:/x")
        self.assertEqual(as_url("view-source:https://a.com"), "view-source:https://a.com")
        self.assertEqual(as_url("chrome://newtab"), "chrome://newtab")
        self.assertEqual(as_url("data:text/html,x"), "data:text/html,x")

    def test_full_scheme_passes_through(self):
        self.assertEqual(as_url("http://x.com"), "http://x.com")
        self.assertEqual(as_url("HTTPS://Example.com"), "HTTPS://Example.com")
        self.assertEqual(as_url("ftp://files.example.com"), "ftp://files.example.com")

    def test_whitespace_returns_empty(self):
        self.assertEqual(as_url("foo bar"), "")
        self.assertEqual(as_url("http://x y"), "")
        self.assertEqual(as_url("   "), "")

    def test_search_query_returns_empty(self):
        self.assertEqual(as_url("singleword"), "")
        self.assertEqual(as_url("Hacker News - Google Chrome"), "")

    def test_empty_returns_empty(self):
        self.assertEqual(as_url(""), "")
        self.assertEqual(as_url(None), "")

    def test_oversize_returns_empty(self):
        # Boundary: exactly 2048 is kept, 2049 is dropped.
        base = "https://" + "a" * 2040
        self.assertEqual(len(base), 2048)
        self.assertTrue(as_url(base).startswith("https://"))
        self.assertEqual(as_url(base + "a"), "")

    def test_strips_null_and_surrounding_space(self):
        self.assertEqual(as_url("  github.com  "), "https://github.com")
        self.assertEqual(as_url("github.com\x00"), "https://github.com")


class StripWindowSuffixTests(unittest.TestCase):
    def test_chrome_suffix(self):
        self.assertEqual(strip_window_suffix("Hacker News - Google Chrome"), "Hacker News")

    def test_firefox_suffix(self):
        self.assertEqual(strip_window_suffix("My Page — Mozilla Firefox"), "My Page")
        self.assertEqual(strip_window_suffix("My Page – Mozilla Firefox"), "My Page")

    def test_profile_suffix(self):
        self.assertEqual(strip_window_suffix("My Page — Profile 4 — Mozilla Firefox"), "My Page")
        self.assertEqual(strip_window_suffix("Tab — Profile 2 - Google Chrome"), "Tab")

    def test_edge_suffix(self):
        # The source pattern spells Edge as "Microsoft\u00a0?Edge": a literal NBSP
        # is optional between "Microsoft" and "Edge" (Windows uses \xa0 there).
        self.assertEqual(strip_window_suffix("Docs – Microsoft\u00a0Edge"), "Docs")
        self.assertEqual(strip_window_suffix("Docs – MicrosoftEdge"), "Docs")

    def test_plain_title_untouched(self):
        self.assertEqual(strip_window_suffix("plain title"), "plain title")

    def test_empty_title(self):
        self.assertEqual(strip_window_suffix(""), "")

    def test_none_title(self):
        self.assertEqual(strip_window_suffix(None), "")


class ActivityPropertyTests(unittest.TestCase):
    def test_browser_resolves_executable_stem(self):
        self.assertEqual(Activity(app="chrome").browser, "chrome")
        self.assertEqual(Activity(app="Chrome").browser, "chrome")
        self.assertEqual(Activity(app="firefox").browser, "firefox")
        self.assertEqual(Activity(app="msedge").browser, "chrome")

    def test_browser_is_none_for_non_browser(self):
        self.assertIsNone(Activity(app="Notepad").browser)
        self.assertIsNone(Activity(app="").browser)

    def test_domain_strips_www(self):
        self.assertEqual(Activity(app="chrome", url="https://www.example.com/a").domain, "example.com")
        self.assertEqual(Activity(app="chrome", url="https://example.com").domain, "example.com")
        self.assertEqual(Activity(app="chrome", url="http://localhost:3000").domain, "localhost")
        self.assertEqual(Activity(app="chrome", url="").domain, "")

    def test_page_drops_fragment(self):
        self.assertEqual(Activity(app="chrome", url="https://x.com/a?x=1#frag").page,
                         "https://x.com/a?x=1")
        self.assertEqual(Activity(app="chrome", url="").page, "")

    def test_label_browser_with_url(self):
        act = Activity(app="chrome", url="https://www.example.com/a", tab_title="Page Title")
        self.assertEqual(act.label, "example.com — Page Title")

    def test_label_browser_with_url_falls_back_to_url(self):
        act = Activity(app="chrome", url="https://example.com/a", tab_title="")
        self.assertEqual(act.label, "example.com — https://example.com/a")

    def test_label_plain_app_with_window_title(self):
        act = Activity(app="Notepad", window_title="readme.txt")
        self.assertEqual(act.label, "Notepad — readme.txt")

    def test_label_plain_app_without_window_title(self):
        act = Activity(app="Notepad")
        self.assertEqual(act.label, "Notepad")

    def test_browser_without_url_uses_app_and_window_title(self):
        act = Activity(app="firefox", window_title="My Page — Mozilla Firefox")
        self.assertEqual(act.label, "firefox — My Page — Mozilla Firefox")


if __name__ == "__main__":
    unittest.main()
