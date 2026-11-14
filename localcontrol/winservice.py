"""Task Scheduler installation with a watchdog, so the monitor is hard to switch off mid-rule.

Two scheduled tasks keep each other alive:
  LocalControl\\Agent  runs the monitor (restarted by Windows if it dies)
  LocalControl\\Guard  re-creates and re-runs the agent if its task is deleted, stopped, or disabled
and the monitor itself re-creates/re-runs the guard. Pristine XML copies live in
~/.localcontrol/tasks/ so deleted tasks get restored, and disabled tasks are re-enabled.
"""

import csv
import io
import os
import subprocess
import sys
import time
from pathlib import Path
from xml.sax.saxutils import escape

from .config import DATA_DIR

AGENT = "LocalControl\\Agent"
GUARD = "LocalControl\\Guard"
TASKS_DIR = DATA_DIR / "tasks"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CREATE_NO_WINDOW = 0x08000000

TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{description}</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <WakeToRun>false</WakeToRun>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{cwd}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _backup(task: str) -> Path:
    """Backup XML path for a task.

    Task names contain a backslash (``LocalControl\\Agent``), which Windows
    treats as a path separator; flatten it so the file stays directly in
    ``TASKS_DIR``.
    """
    return TASKS_DIR / (task.replace("\\", "-") + ".xml")


def _python() -> Path:
    """Console-less interpreter, so the monitor never flashes a terminal."""
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return pythonw if pythonw.exists() else exe


def _user() -> str:
    domain, name = os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", "")
    return f"{domain}\\{name}" if domain and name else name


def _xml(arguments: str, description: str) -> str:
    return TASK_XML.format(
        description=escape(description),
        user=escape(_user()),
        command=escape(str(_python())),
        arguments=escape(arguments),
        cwd=escape(str(PROJECT_ROOT)),
    )


def _tasks(port: int) -> dict[str, tuple[str, str]]:
    """name -> (XML, description) for the two cooperating tasks."""
    return {
        AGENT: (_xml(f"-m localcontrol --port {port} serve --no-open --service",
                     "LocalControl monitor: judges the foreground window against your rule."),
                "LocalControl monitor"),
        GUARD: (_xml("-m localcontrol guard",
                     "LocalControl watchdog: keeps the monitor task installed and running."),
                "LocalControl watchdog"),
    }


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, check=False,
                          creationflags=CREATE_NO_WINDOW)


def status(task: str) -> str:
    """'Running' / 'Ready' / 'Disabled', or '' when the task doesn't exist.

    schtasks reports its status in the system language, so fall back to the
    CIM property (whose enum names are always English) when the CSV value is
    not one we recognise.
    """
    r = _schtasks("/Query", "/TN", task, "/FO", "CSV", "/NH")
    if r.returncode != 0:
        return ""
    for row in csv.reader(io.StringIO(r.stdout)):
        if len(row) >= 3 and row[0] == task:
            state = row[2].strip()
            return state if state in ("Running", "Ready", "Disabled", "Queued") else _cim_state(task) or state
    return ""


def _cim_state(task: str) -> str:
    """The task state as an English enum name (Get-ScheduledTask), or ''."""
    script = (f"$t = Get-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue; "
              "if ($t) { $t.State }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, text=True, check=False,
                           creationflags=CREATE_NO_WINDOW)
    except OSError:
        return ""
    return r.stdout.strip()


def _create(task: str, xml_path: Path) -> bool:
    r = _schtasks("/Create", "/TN", task, "/XML", str(xml_path), "/F")
    if r.returncode != 0:
        raise OSError(f"schtasks /Create {task} failed: {(r.stderr or r.stdout).strip()}")
    return True


def install(port: int) -> None:
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    for task, (xml, _description) in _tasks(port).items():
        path = _backup(task)
        # Atomic (a half-written backup would break restore) and UTF-16, the
        # encoding Task Scheduler expects for /XML, regardless of locale.
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(xml, encoding="utf-16")
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        _schtasks("/End", "/TN", task)
        _create(task, path)
    _schtasks("/Run", "/TN", GUARD)


def installed() -> bool:
    return _backup(GUARD).exists()


def _re_enable(task: str) -> str:
    """Turn a disabled task back on; report its state afterwards.

    ``schtasks /Run`` does nothing on a disabled task, so ``/Change /ENABLE``
    comes first, then ``/Run`` if it is Ready but not already running.
    """
    _schtasks("/Change", "/TN", task, "/ENABLE")
    state = status(task)
    if state in ("Ready", "Queued"):
        _schtasks("/Run", "/TN", task)
        state = status(task)
    return state


def ensure_guard() -> None:
    """Called periodically by the monitor when running as the service.

    A disabled guard task is treated as broken: re-enable it, then start it
    if it is Ready but not running. Only a Running/Ready/Queued guard (or no
    backup to restore from) short-circuits.
    """
    if not installed():
        return
    state = status(GUARD)
    if state == "Disabled":
        _re_enable(GUARD)
        return
    if state in ("Running", "Ready", "Queued"):
        return
    # "" means missing -> recreate from the backup.
    backup = _backup(GUARD)
    try:
        _create(GUARD, backup)
        _schtasks("/Run", "/TN", GUARD)
    except OSError:
        pass


def guard_loop(poll: float = 5.0, log: Path | None = None) -> None:
    """Keep the agent task installed and running; the agent does the same for us."""
    log = log or DATA_DIR / "guard.log"

    def note(message: str):
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
        except OSError:
            pass

    while True:
        try:
            state = status(AGENT)
            if not state:
                backup = _backup(AGENT)
                if backup.exists():
                    _create(AGENT, backup)
                    note("agent task was missing; recreated")
                    _schtasks("/Run", "/TN", AGENT)
            elif state == "Disabled":
                _schtasks("/Change", "/TN", AGENT, "/ENABLE")
                _schtasks("/Run", "/TN", AGENT)
                note("agent task was disabled; re-enabled and started it")
            elif state != "Running":
                _schtasks("/Run", "/TN", AGENT)
                note(f"agent was {state}; started it")
        except OSError as e:
            note(f"guard error: {e}")
        time.sleep(poll)


def uninstall() -> None:
    # Remove the backups first so neither side restores the other mid-teardown.
    for task in (GUARD, AGENT):
        _backup(task).unlink(missing_ok=True)
    for task in (GUARD, AGENT):
        _schtasks("/End", "/TN", task)
        _schtasks("/Delete", "/TN", task, "/F")
