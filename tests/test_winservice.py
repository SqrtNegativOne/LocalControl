"""Tests for localcontrol.winservice: schtasks CSV status parsing and the
watchdog's Disabled-task recovery (ensure_guard / guard_loop)."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import localcontrol.winservice as ws

GUARD = ws.GUARD
AGENT = ws.AGENT


class _Result:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _status(stdout, returncode=0, cim=""):
    with mock.patch.object(ws, "_schtasks", return_value=_Result(stdout, returncode)), \
         mock.patch.object(ws, "_cim_state", return_value=cim):
        return ws.status(GUARD)


class StatusParsingTests(unittest.TestCase):
    def test_running(self):
        self.assertEqual(_status(f"{GUARD},,Running\n"), "Running")

    def test_ready(self):
        self.assertEqual(_status(f"{GUARD},,Ready\n"), "Ready")

    def test_disabled(self):
        self.assertEqual(_status(f"{GUARD},,Disabled\n"), "Disabled")

    def test_quoted_fields(self):
        self.assertEqual(_status(f'"{GUARD}","2024-01-01","Queued"\n'), "Queued")

    def test_nonzero_returncode_is_missing(self):
        self.assertEqual(_status("", returncode=1), "")

    def test_garbled_csv_is_empty(self):
        self.assertEqual(_status("nonsense here\n"), "")

    def test_wrong_task_row_is_empty(self):
        self.assertEqual(_status("Other\\Task,,Ready\n"), "")

    def test_localized_value_falls_back_to_cim(self):
        # The CSV says a localized word; the CIM enum name is English.
        self.assertEqual(_status(f"{GUARD},,Bereit\n", cim="Ready"), "Ready")

    def test_localized_value_with_no_cim_falls_back_to_raw(self):
        self.assertEqual(_status(f"{GUARD},,Bereit\n", cim=""), "Bereit")

    def test_status_uses_schtasks_query(self):
        # Ensure the query shape (not a /Create or /Run) reaches schtasks.
        seen = []

        def fake(*args):
            seen.append(args)
            return _Result(f"{GUARD},,Ready\n")

        with mock.patch.object(ws, "_schtasks", side_effect=fake):
            ws.status(GUARD)
        self.assertEqual(seen, [("/Query", "/TN", GUARD, "/FO", "CSV", "/NH")])


class EnsureGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.tasks = self.tmp / "tasks"
        self.tasks.mkdir(parents=True)
        # installed() checks the backup XML exists.
        (self.tasks / "LocalControl-Guard.xml").write_text("<Task/>")
        self.calls = []

    def _run(self, state):
        def fake_schtasks(*args):
            self.calls.append(args)
            return _Result()

        with mock.patch.object(ws, "TASKS_DIR", self.tasks), \
             mock.patch.object(ws, "_schtasks", side_effect=fake_schtasks), \
             mock.patch.object(ws, "status", return_value=state):
            ws.ensure_guard()

    def test_disabled_guard_is_re_enabled(self):
        self._run("Disabled")
        self.assertEqual(self.calls, [("/Change", "/TN", GUARD, "/ENABLE")])

    def test_running_guard_is_left_alone(self):
        self._run("Running")
        self.assertEqual(self.calls, [])

    def test_ready_guard_is_left_alone(self):
        self._run("Ready")
        self.assertEqual(self.calls, [])

    def test_queued_guard_is_left_alone(self):
        self._run("Queued")
        self.assertEqual(self.calls, [])

    def test_missing_guard_is_recreated_from_backup(self):
        self._run("")
        self.assertEqual([c[0] for c in self.calls], ["/Create", "/Run"])

    def test_no_backup_does_nothing(self):
        (self.tasks / "LocalControl-Guard.xml").unlink()
        self._run("")
        self.assertEqual(self.calls, [])


class GuardLoopTests(unittest.TestCase):
    """guard_loop is an infinite loop; stop it after one iteration."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.log = self.tmp / "guard.log"
        self.calls = []

    def _run_one_iteration(self, agent_state):
        class _Stop(Exception):
            pass

        def fake_schtasks(*args):
            self.calls.append(args)
            return _Result()

        def fake_sleep(_poll):
            raise _Stop()

        with mock.patch.object(ws, "_schtasks", side_effect=fake_schtasks), \
             mock.patch.object(ws, "status", return_value=agent_state), \
             mock.patch.object(ws.time, "sleep", side_effect=fake_sleep):
            try:
                ws.guard_loop(poll=0.0, log=self.log)
            except _Stop:
                pass

    def test_disabled_agent_is_enabled_and_started(self):
        self._run_one_iteration("Disabled")
        self.assertEqual(self.calls,
                         [("/Change", "/TN", AGENT, "/ENABLE"), ("/Run", "/TN", AGENT)])
        self.assertIn("agent task was disabled", self.log.read_text())

    def test_ready_agent_is_not_touched(self):
        # A Ready (not Running) agent is started, matching the old behavior.
        self._run_one_iteration("Ready")
        self.assertEqual(self.calls, [("/Run", "/TN", AGENT)])
        self.assertIn("agent was Ready", self.log.read_text())

    def test_running_agent_is_left_alone(self):
        self._run_one_iteration("Running")
        self.assertEqual(self.calls, [])

    def test_missing_agent_is_recreated(self):
        (self.tmp / "tasks").mkdir(parents=True, exist_ok=True)
        (self.tmp / "tasks" / "LocalControl-Agent.xml").write_text("<Task/>")
        with mock.patch.object(ws, "TASKS_DIR", self.tmp / "tasks"):
            self._run_one_iteration("")
        self.assertEqual([c[0] for c in self.calls], ["/Create", "/Run"])
        self.assertIn("agent task was missing", self.log.read_text())


if __name__ == "__main__":
    unittest.main()
