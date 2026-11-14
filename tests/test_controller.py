"""Tests for localcontrol.controller: cache keys, duration fallback, the
appeal round trip, and fail-closed verdict validation."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import localcontrol.controller as cmod
from localcontrol.config import Config
from localcontrol.controller import (
    Controller,
    Policy,
    Verdict,
    appeal_activity,
    block_activity,
    cache_keys,
    fallback_minutes,
)
from localcontrol.desktop import Activity
from localcontrol.llm import LLMError

# A judge response the model would return for a normal judgment.
JUDGE_OK = {"reason": "fits the rule", "allowed": True, "scope": "page"}
# The schema distinguishes the parse call (set_policy) from a judge call.
PARSE_SCHEMA_KEYS = ("duration_minutes", "rule")


class FakeLLM:
    """Duck-typed stand-in for Ollama/Llama: no network, controllable replies."""

    def __init__(self, judge=JUDGE_OK, parse=None):
        self.judge = judge          # dict | Exception | callable(user) -> dict
        self.parse = parse          # reply to the set_policy parse call
        self.judge_calls = 0
        self.parse_calls = 0

    def vision(self):
        return None

    def status(self):
        return True, "fake ready"

    def revive(self):
        pass

    def chat_json(self, system, user, schema, images=()):
        if "duration_minutes" in schema.get("properties", {}):
            self.parse_calls += 1
            if self.parse is None:
                raise AssertionError("unexpected set_policy parse call")
            if isinstance(self.parse, Exception):
                raise self.parse
            return self.parse
        self.judge_calls += 1
        if isinstance(self.judge, Exception):
            raise self.judge
        if callable(self.judge):
            return self.judge(user)
        return self.judge


def make_controller(tmp: Path, judge=JUDGE_OK, parse=None, **cfg_kw) -> tuple[Controller, FakeLLM]:
    """Build a Controller whose model is a FakeLLM and whose data_dir is tmp."""
    cfg = Config(backend="ollama", **cfg_kw)
    llm = FakeLLM(judge, parse)
    with mock.patch.object(cmod, "make_llm", return_value=llm):
        ctl = Controller(cfg, data_dir=tmp)
    return ctl, llm


class CacheKeysTests(unittest.TestCase):
    def test_browser_with_url(self):
        act = Activity(app="chrome", url="https://x.com/home", tab_title="Home",
                       window_title="x - Google Chrome")
        self.assertEqual(cache_keys(act), ("site:x.com", "page:https://x.com/home"))

    def test_browser_without_url_uses_tab_title(self):
        act = Activity(app="firefox", window_title="Mozilla Firefox", tab_title="Hacker News")
        self.assertEqual(cache_keys(act), ("app:firefox|Hacker News", "win:firefox|Hacker News"))

    def test_plain_app_uses_window_title(self):
        act = Activity(app="Notepad", window_title="readme.txt")
        self.assertEqual(cache_keys(act), ("app:Notepad", "win:Notepad|readme.txt"))

    def test_plain_app_without_window_title(self):
        act = Activity(app="Notepad")
        self.assertEqual(cache_keys(act), ("app:Notepad", "win:Notepad|"))


class FallbackMinutesTests(unittest.TestCase):
    def test_half_an_hour(self):
        self.assertEqual(fallback_minutes("half an hour"), 30)
        self.assertEqual(fallback_minutes("Half an hour"), 30)

    def test_for_90_minutes(self):
        self.assertEqual(fallback_minutes("for 90 minutes"), 90.0)

    def test_two_hours(self):
        self.assertEqual(fallback_minutes("2 hours"), 120.0)
        self.assertEqual(fallback_minutes("1.5 hrs"), 90.0)

    def test_no_duration(self):
        self.assertEqual(fallback_minutes("no duration"), 0)
        self.assertEqual(fallback_minutes("just do it"), 0)

    def test_the_next_hour(self):
        self.assertEqual(fallback_minutes("for the next hour"), 60.0)


class VerdictValidationTests(unittest.TestCase):
    """Controller._verdict must fail closed on any malformed model answer."""

    def test_missing_allowed_raises(self):
        with self.assertRaises(LLMError):
            Controller._verdict({"reason": "x", "scope": "page"})

    def test_non_bool_allowed_raises(self):
        for bad in ("yes", 1, 0, None, [], {}):
            with self.subTest(bad=bad), self.assertRaises(LLMError):
                Controller._verdict({"allowed": bad, "reason": "x", "scope": "page"})

    def test_missing_reason_raises(self):
        with self.assertRaises(LLMError):
            Controller._verdict({"allowed": True, "scope": "page"})

    def test_non_string_reason_raises(self):
        with self.assertRaises(LLMError):
            Controller._verdict({"allowed": True, "reason": 5, "scope": "page"})

    def test_invalid_scope_raises(self):
        for bad in ("widget", "", None, 1):
            with self.subTest(bad=bad), self.assertRaises(LLMError):
                Controller._verdict({"allowed": True, "reason": "x", "scope": bad})

    def test_non_dict_raises(self):
        for bad in ("text", None, ["allowed"], 42):
            with self.subTest(bad=bad), self.assertRaises(LLMError):
                Controller._verdict(bad)

    def test_valid_dict_accepted(self):
        v = Controller._verdict({"allowed": False, "reason": "no", "scope": "site"})
        self.assertIsInstance(v, Verdict)
        self.assertFalse(v.allowed)
        self.assertEqual(v.reason, "no")
        self.assertEqual(v.scope, "site")
        self.assertEqual(v.source, "llm")


class EvaluateFailClosedTests(unittest.TestCase):
    def test_missing_allowed_fails_closed(self):
        """A parseable answer without `allowed` blocks, does not raise, and is not cached."""
        tmp = Path(tempfile.mkdtemp())
        # Missing `allowed` -> _verdict raises LLMError -> evaluate returns an error verdict.
        ctl, llm = make_controller(tmp, judge={"reason": "x", "scope": "page"})
        ctl.policy = Policy(command="c", rule="r", started_at=0.0, expires_at=1e18)
        act = Activity(app="Notepad", window_title="readme.txt")
        v = ctl.evaluate(act)
        self.assertIsInstance(v, Verdict)
        self.assertFalse(v.allowed)
        self.assertEqual(v.source, "error")
        self.assertEqual(llm.judge_calls, 1)
        self.assertTrue(ctl.last_error)          # error surfaced
        self.assertEqual(dict(ctl.cache), {})     # not cached, so it recovers later

    def test_non_bool_allowed_fails_closed(self):
        tmp = Path(tempfile.mkdtemp())
        ctl, _llm = make_controller(tmp, judge={"allowed": "yes", "reason": "x", "scope": "page"})
        ctl.policy = Policy(command="c", rule="r", started_at=0.0, expires_at=1e18)
        v = ctl.evaluate(Activity(app="Notepad", window_title="readme.txt"))
        self.assertFalse(v.allowed)
        self.assertEqual(v.source, "error")

    def test_model_exception_fails_closed(self):
        tmp = Path(tempfile.mkdtemp())
        ctl, _llm = make_controller(tmp, judge=LLMError("model down"))
        ctl.policy = Policy(command="c", rule="r", started_at=0.0, expires_at=1e18)
        v = ctl.evaluate(Activity(app="Notepad", window_title="readme.txt"))
        self.assertFalse(v.allowed)
        self.assertEqual(v.source, "error")

    def test_valid_answer_is_allowed_and_cached(self):
        tmp = Path(tempfile.mkdtemp())
        ctl, llm = make_controller(tmp, judge=JUDGE_OK)
        ctl.policy = Policy(command="c", rule="r", started_at=0.0, expires_at=1e18)
        act = Activity(app="Notepad", window_title="readme.txt")
        v = ctl.evaluate(act)
        self.assertTrue(v.allowed)
        self.assertEqual(v.source, "llm")
        # Second evaluate is served from the cache: no extra model call.
        v2 = ctl.evaluate(act)
        self.assertEqual(v2.source, "cache")
        self.assertEqual(llm.judge_calls, 1)

    def test_builtin_answers_short_circuit_the_model(self):
        tmp = Path(tempfile.mkdtemp())
        ctl, llm = make_controller(tmp)
        ctl.policy = Policy(command="c", rule="r", started_at=0.0, expires_at=1e18)
        v = ctl.evaluate(Activity(app="explorer", window_title="x"))
        self.assertTrue(v.allowed)
        self.assertEqual(v.source, "builtin")
        self.assertEqual(llm.judge_calls, 0)


class AppealRoundTripTests(unittest.TestCase):
    """Block a Firefox tab (no URL), appeal by block_id, then evaluate must
    honor the override without calling the model again."""

    def test_block_id_appeal_matches_evaluate(self):
        tmp = Path(tempfile.mkdtemp())
        # Denied judgment so enforce() records the block.
        ctl, llm = make_controller(tmp, judge={"reason": "nope", "allowed": False, "scope": "page"})
        ctl.policy = Policy(command="c", rule="only papers", started_at=0.0, expires_at=1e18)

        act = Activity(app="firefox", window_title="Mozilla Firefox", tab_title="Hacker News")
        with mock.patch.object(cmod.desktop, "redirect_tab") as redirect:
            ctl.enforce(act, Verdict(False, "nope"))
        redirect.assert_called_once()
        self.assertEqual(len(ctl.blocks), 1)
        block_id = next(iter(ctl.blocks))
        # Firefox tabs have no URL; the block records it.
        self.assertEqual(ctl.blocks[block_id]["url"], "")

        # The appeal is allowed.
        llm.judge = {"reason": "fine", "allowed": True, "scope": "page"}
        v = ctl.appeal("", "Hacker News", "I need this", block_id=block_id)
        self.assertTrue(v.allowed)
        self.assertEqual(v.source, "appeal")

        # The override landed under exactly the key cache_keys computes for the live activity.
        _site, page = cache_keys(act)
        self.assertIn(page, ctl.policy.overrides)
        self.assertEqual(ctl.policy.overrides[page], "I need this")

        # A subsequent evaluate must return the appeal verdict with NO model call.
        def boom(*_a, **_k):
            raise AssertionError("model called after the appeal override")

        llm.chat_json = boom
        v2 = ctl.evaluate(act)
        self.assertTrue(v2.allowed)
        self.assertEqual(v2.source, "appeal")
        self.assertEqual(v2.reason, "Allowed by your appeal")

    def test_no_block_id_appeal_matches_by_url_and_title(self):
        tmp = Path(tempfile.mkdtemp())
        ctl, llm = make_controller(tmp, judge={"reason": "nope", "allowed": False, "scope": "page"})
        ctl.policy = Policy(command="c", rule="only papers", started_at=0.0, expires_at=1e18)

        act = Activity(app="chrome", url="https://x.com/home", tab_title="Home", window_title="x")
        with mock.patch.object(cmod.desktop, "hide_app"), \
             mock.patch.object(cmod.desktop, "notify"):
            ctl.enforce(act, Verdict(False, "nope"))

        # Legacy block page: no block_id, matched by the posted url + title.
        llm.judge = {"reason": "fine", "allowed": True, "scope": "page"}
        v = ctl.appeal("https://x.com/home", "Home", "need it")
        self.assertTrue(v.allowed)
        self.assertEqual(v.source, "appeal")

        _, page = cache_keys(act)
        self.assertIn(page, ctl.policy.overrides)

        def boom(*_a, **_k):
            raise AssertionError("model called after the appeal override")

        llm.chat_json = boom
        v2 = ctl.evaluate(act)
        self.assertTrue(v2.allowed)
        self.assertEqual(v2.source, "appeal")

    def test_appeal_without_policy_is_builtin_allow(self):
        tmp = Path(tempfile.mkdtemp())
        ctl, llm = make_controller(tmp)
        self.assertIsNone(ctl.policy)
        v = ctl.appeal("", "", "any")
        self.assertTrue(v.allowed)
        self.assertEqual(v.source, "builtin")
        self.assertEqual(llm.judge_calls, 0)

    def test_appeal_denied_writes_no_override(self):
        tmp = Path(tempfile.mkdtemp())
        ctl, _llm = make_controller(tmp, judge={"reason": "nope", "allowed": False, "scope": "page"})
        ctl.policy = Policy(command="c", rule="r", started_at=0.0, expires_at=1e18)
        v = ctl.appeal("", "Hacker News", "let me", block_id=None)
        self.assertFalse(v.allowed)
        self.assertEqual(dict(ctl.policy.overrides), {})

    def test_appeal_with_unknown_block_id_falls_back_to_guess(self):
        # No matching entry -> chrome-shaped guess; the override key must still
        # be one evaluate() can match when the live activity matches the guess.
        blocks = {"b1": {"url": "https://x.com/home", "title": "Home",
                         "activity": {"app": "chrome", "url": "https://x.com/home",
                                      "tab_title": "Home", "window_title": "x"}}}
        act = appeal_activity(blocks, "https://x.com/home", "Home", block_id="b1")
        self.assertEqual(act.app, "chrome")
        self.assertEqual(act.url, "https://x.com/home")
        # Unknown id falls back to the chrome-shaped guess.
        guess = appeal_activity(blocks, "https://other.com", "Other", block_id="missing")
        self.assertEqual(guess.app, "Google Chrome")
        self.assertEqual(guess.url, "https://other.com")
        self.assertEqual(guess.tab_title, "Other")

    def test_block_activity_requires_activity_dict(self):
        self.assertIsNone(block_activity(None))
        self.assertIsNone(block_activity({}))
        self.assertIsNone(block_activity({"url": "u", "title": "t"}))
        self.assertIsNone(block_activity({"activity": {}}))
        self.assertIsNone(block_activity({"activity": "not-a-dict"}))
        act = block_activity({"activity": {"app": "chrome", "url": "https://a.b",
                                           "tab_title": "t", "window_title": "w"}})
        self.assertEqual(act.app, "chrome")
        self.assertEqual(act.url, "https://a.b")


class SetPolicyTests(unittest.TestCase):
    def test_set_policy_uses_model_parse(self):
        tmp = Path(tempfile.mkdtemp())
        ctl, llm = make_controller(
            tmp, parse={"duration_minutes": 90, "rule": "only papers"})
        p = ctl.set_policy("work on papers for 90 minutes")
        self.assertEqual(p.rule, "only papers")
        self.assertAlmostEqual(p.expires_at - p.started_at, 90 * 60, delta=2)
        self.assertEqual(llm.parse_calls, 1)

    def test_set_policy_falls_back_when_parse_fails(self):
        tmp = Path(tempfile.mkdtemp())
        ctl, _llm = make_controller(tmp, parse=LLMError("bad json"))
        p = ctl.set_policy("half an hour of deep work")
        # LLM parse failed -> fallback_minutes("half an hour") = 30 minutes.
        self.assertAlmostEqual(p.expires_at - p.started_at, 30 * 60, delta=2)
        self.assertTrue(ctl.last_error)

    def test_set_policy_is_locked_while_active(self):
        from localcontrol.controller import PolicyLocked

        tmp = Path(tempfile.mkdtemp())
        ctl, _llm = make_controller(tmp, parse={"duration_minutes": 60, "rule": "r"})
        ctl.set_policy("one hour")
        with self.assertRaises(PolicyLocked):
            ctl.set_policy("another hour")

    def test_policy_survives_reload(self):
        tmp = Path(tempfile.mkdtemp())
        ctl, _llm = make_controller(tmp, parse={"duration_minutes": 60, "rule": "keep me"})
        ctl.set_policy("keep me for an hour")
        # Build a second controller over the same data dir; the policy must reload.
        with mock.patch.object(cmod, "make_llm", return_value=FakeLLM()):
            ctl2 = Controller(Config(backend="ollama"), data_dir=tmp)
        self.assertIsNotNone(ctl2.policy)
        self.assertEqual(ctl2.policy.rule, "keep me")
        self.assertEqual(ctl2.policy.id, ctl.policy.id)
        self.assertEqual(ctl2.policy.overrides, ctl.policy.overrides)

    def test_set_policy_records_history_in_injected_data_dir(self):
        tmp = Path(tempfile.mkdtemp())
        ctl, _llm = make_controller(tmp, parse={"duration_minutes": 60, "rule": "r"})
        ctl.set_policy("one hour")
        # PromptHistory must follow data_dir, not the real ~/.localcontrol.
        self.assertTrue((tmp / "prompts.json").exists())


class ScreenshotDecisionTests(unittest.TestCase):
    def controller(self, vision: str, vision_supported: bool | None):
        tmp = Path(tempfile.mkdtemp())
        ctl, _llm = make_controller(tmp, vision=vision)
        ctl.vision_supported = vision_supported
        return ctl

    def test_off_never_captures(self):
        ctl = self.controller("off", True)
        self.assertFalse(ctl._wants_screenshot(Activity(app="UnknownApp")))

    def test_model_without_vision_never_captures(self):
        ctl = self.controller("always", False)
        self.assertFalse(ctl._wants_screenshot(Activity(app="UnknownApp")))

    def test_always_captures_even_with_text_context(self):
        ctl = self.controller("always", True)
        act = Activity(app="chrome", url="https://example.com", tab_title="Example",
                       window_title="Example - Google Chrome")
        self.assertTrue(ctl._wants_screenshot(act))

    def test_auto_captures_browser_without_url(self):
        ctl = self.controller("auto", True)
        self.assertTrue(ctl._wants_screenshot(Activity(app="firefox", tab_title="Hacker News")))

    def test_auto_skips_browser_with_url(self):
        ctl = self.controller("auto", True)
        act = Activity(app="chrome", url="https://docs.python.org/3/", tab_title="Python docs",
                       window_title="Python docs - Google Chrome")
        self.assertFalse(ctl._wants_screenshot(act))

    def test_auto_captures_non_browser_without_title(self):
        ctl = self.controller("auto", True)
        self.assertTrue(ctl._wants_screenshot(Activity(app="UnknownApp")))

    def test_auto_skips_non_browser_with_title(self):
        ctl = self.controller("auto", True)
        self.assertFalse(ctl._wants_screenshot(Activity(app="Acrobat", window_title="paper.pdf")))


if __name__ == "__main__":
    unittest.main()
