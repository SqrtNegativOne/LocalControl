"""Tests for persistence: atomic writes, Policy round trips, and PromptHistory."""

import json
import tempfile
import unittest
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import localcontrol.config as config_mod
import localcontrol.controller as controller_mod
import localcontrol.history as history_mod
from localcontrol.config import Config, atomic_write_text
from localcontrol.controller import Controller, Policy, Usage
from localcontrol.history import PromptHistory


class AtomicWriteTextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "state.json"

    def test_writes_content(self):
        atomic_write_text(self.path, '{"policy": null}')
        self.assertEqual(self.path.read_text(), '{"policy": null}')

    def test_leaves_no_tmp_on_success(self):
        atomic_write_text(self.path, "hello")
        self.assertTrue(self.path.exists())
        self.assertFalse(self.path.with_name(self.path.name + ".tmp").exists())

    def test_failed_replace_leaves_original_and_no_tmp(self):
        atomic_write_text(self.path, "original")
        with mock.patch.object(config_mod.os, "replace", side_effect=OSError("boom")), \
             self.assertRaises(OSError):
            atomic_write_text(self.path, "new content")
        self.assertEqual(self.path.read_text(), "original")
        self.assertFalse(self.path.with_name(self.path.name + ".tmp").exists())

    def test_failed_replace_on_missing_target_leaves_no_tmp(self):
        with mock.patch.object(config_mod.os, "replace", side_effect=OSError("boom")), \
             self.assertRaises(OSError):
            atomic_write_text(self.path, "new content")
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.with_name(self.path.name + ".tmp").exists())


class PolicyRoundTripTests(unittest.TestCase):
    """A Policy survives asdict -> json -> _load_policy with no data loss."""

    def test_asdict_json_load_round_trip(self):
        tmp = Path(tempfile.mkdtemp())
        from tests.test_controller import FakeLLM

        with mock.patch("localcontrol.controller.make_llm", return_value=FakeLLM()):
            ctl = Controller(Config(backend="ollama"), data_dir=tmp)

        import time as _time

        now = _time.time()
        p = Policy(command="only papers", rule="You may only read papers",
                   started_at=now, expires_at=now + 5400.0,
                   overrides={"page:https://arxiv.org/a": "research"})
        ctl.policy = p
        ctl._save_policy()

        raw = json.loads((tmp / "state.json").read_text())["policy"]
        self.assertEqual(raw["command"], "only papers")
        self.assertEqual(raw["overrides"], {"page:https://arxiv.org/a": "research"})
        self.assertEqual(raw, asdict(p))

        with mock.patch("localcontrol.controller.make_llm", return_value=FakeLLM()):
            ctl2 = Controller(Config(backend="ollama"), data_dir=tmp)
        loaded = ctl2.policy
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.id, p.id)
        self.assertEqual(loaded.command, p.command)
        self.assertEqual(loaded.rule, p.rule)
        self.assertEqual(loaded.started_at, p.started_at)
        self.assertEqual(loaded.expires_at, p.expires_at)
        self.assertEqual(loaded.overrides, p.overrides)

    def test_none_policy_saves_null(self):
        tmp = Path(tempfile.mkdtemp())
        from tests.test_controller import FakeLLM

        with mock.patch("localcontrol.controller.make_llm", return_value=FakeLLM()):
            ctl = Controller(Config(backend="ollama"), data_dir=tmp)
        self.assertIsNone(ctl.policy)
        ctl._save_policy()
        self.assertIsNone(json.loads((tmp / "state.json").read_text())["policy"])

    def test_corrupt_state_loads_none(self):
        tmp = Path(tempfile.mkdtemp())
        tmp.mkdir(parents=True, exist_ok=True)
        (tmp / "state.json").write_text("{not json")
        from tests.test_controller import FakeLLM

        with mock.patch("localcontrol.controller.make_llm", return_value=FakeLLM()):
            ctl = Controller(Config(backend="ollama"), data_dir=tmp)
        self.assertIsNone(ctl.policy)

    def _controller(self, tmp: Path) -> Controller:
        from tests.test_controller import FakeLLM

        with mock.patch("localcontrol.controller.make_llm", return_value=FakeLLM()):
            return Controller(Config(backend="ollama"), data_dir=tmp)

    def test_backup_restores_a_deleted_live_state(self):
        tmp = Path(tempfile.mkdtemp())
        ctl = self._controller(tmp)
        p = Policy(command="c", rule="locked", started_at=0.0, expires_at=1e18)
        ctl.policy = p
        ctl._save_policy()
        (tmp / "state.json").unlink()

        reloaded = self._controller(tmp)
        self.assertIsNotNone(reloaded.policy)
        self.assertEqual(reloaded.policy.id, p.id)
        self.assertEqual(reloaded.policy.rule, "locked")

    def test_backup_restores_a_corrupt_live_state(self):
        tmp = Path(tempfile.mkdtemp())
        ctl = self._controller(tmp)
        p = Policy(command="c", rule="locked", started_at=0.0, expires_at=1e18)
        ctl.policy = p
        ctl._save_policy()
        (tmp / "state.json").write_text("{truncated")

        reloaded = self._controller(tmp)
        self.assertIsNotNone(reloaded.policy)
        self.assertEqual(reloaded.policy.id, p.id)

    def test_cleared_policy_does_not_resurrect_from_backup(self):
        tmp = Path(tempfile.mkdtemp())
        ctl = self._controller(tmp)
        ctl.policy = Policy(command="c", rule="locked", started_at=0.0, expires_at=1e18)
        ctl._save_policy()
        ctl.policy = None
        ctl._save_policy()          # the backup must also be cleared
        (tmp / "state.json").unlink()

        reloaded = self._controller(tmp)
        self.assertIsNone(reloaded.policy)

    def test_expired_policy_is_not_loaded(self):
        tmp = Path(tempfile.mkdtemp())
        p = Policy(command="c", rule="r", started_at=0.0, expires_at=1.0)  # long past
        (tmp / "state.json").write_text(json.dumps({"policy": asdict(p)}))
        from tests.test_controller import FakeLLM

        with mock.patch("localcontrol.controller.make_llm", return_value=FakeLLM()):
            ctl = Controller(Config(backend="ollama"), data_dir=tmp)
        self.assertIsNone(ctl.policy)


class PromptHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "prompts.json"        # PromptHistory reads DATA_DIR at construction (which may hold the real
        # user's prompts.json), so build it with DATA_DIR pointed at a temp dir
        # and re-point .path as a belt-and-braces guarantee we never touch it.
        with mock.patch.object(history_mod, "DATA_DIR", self.tmp):
            self.hist = PromptHistory()
        self.hist.path = self.path

    def _fresh(self) -> PromptHistory:
        """A new PromptHistory that reads the same temp file."""
        with mock.patch.object(history_mod, "DATA_DIR", self.tmp):
            fresh = PromptHistory()
        fresh.path = self.path
        return fresh

    def test_record_then_top_round_trip(self):
        self.hist.record("only papers", 60)
        self.hist.record("only papers", 60)   # dedupes into one item, count 2
        self.hist.record("no games", 30)
        top = self.hist.top()
        self.assertEqual(len(top), 2)
        self.assertEqual(top[0]["text"], "only papers")
        self.assertEqual(top[0]["count"], 2)
        self.assertEqual(top[0]["minutes"], 60)
        self.assertEqual(top[1]["text"], "no games")

        # A fresh history over the same file sees the same items.
        fresh = self._fresh()
        self.assertEqual(len(fresh.top()), 2)
        self.assertEqual(fresh.top()[0]["count"], 2)

    def test_record_uses_atomic_write(self):
        with mock.patch.object(history_mod, "atomic_write_text",
                               wraps=atomic_write_text) as spy:
            self.hist.record("x", 10)
        spy.assert_called()
        self.assertFalse(self.path.with_name(self.path.name + ".tmp").exists())

    def test_forget_removes_item(self):
        self.hist.record("only papers", 60)
        self.hist.record("no games", 30)
        self.hist.forget("only papers")
        top = self.hist.top()
        self.assertEqual([i["text"] for i in top], ["no games"])
        fresh = self._fresh()
        self.assertEqual([i["text"] for i in fresh.top()], ["no games"])

    def test_forget_is_case_and_whitespace_insensitive(self):
        self.hist.record("Only Papers", 60)
        self.hist.forget("  only   PAPERS ")
        self.assertEqual(self.hist.top(), [])

    def test_top_orders_by_count_then_recency(self):
        self.hist.record("a", 10)
        self.hist.record("b", 10)
        self.hist.record("a", 10)   # a now has count 2
        top = self.hist.top()
        self.assertEqual(top[0]["text"], "a")
        self.assertEqual(top[0]["count"], 2)

    def test_explicit_data_dir_isolated_from_real_history(self):
        hist = PromptHistory(self.tmp / "elsewhere")
        hist.record("x", 10)
        self.assertTrue((self.tmp / "elsewhere" / "prompts.json").exists())
        self.assertFalse(self.path.exists())


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_save_is_atomic_and_round_trips(self):
        usage = Usage(self.tmp)
        usage.apps = {"chrome": 12.5}
        usage.sites = {"x.com": 5}
        with mock.patch.object(controller_mod, "atomic_write_text",
                               wraps=atomic_write_text) as spy:
            usage.save()
        spy.assert_called_once()
        self.assertFalse(usage.path.with_name(usage.path.name + ".tmp").exists())
        loaded = Usage(self.tmp)
        self.assertEqual(loaded.apps, {"chrome": 12.5})
        self.assertEqual(loaded.sites, {"x.com": 5})

    def test_prune_removes_old_usage_files(self):
        usage_dir = self.tmp / "usage"
        usage_dir.mkdir()
        old = (date.today() - timedelta(days=100)).isoformat()
        recent = (date.today() - timedelta(days=1)).isoformat()
        (usage_dir / f"{old}.json").write_text("{}")
        (usage_dir / f"{recent}.json").write_text("{}")
        (usage_dir / "not-a-date.json").write_text("{}")

        Usage(self.tmp)

        self.assertFalse((usage_dir / f"{old}.json").exists())
        self.assertTrue((usage_dir / f"{recent}.json").exists())
        self.assertTrue((usage_dir / "not-a-date.json").exists())


if __name__ == "__main__":
    unittest.main()
