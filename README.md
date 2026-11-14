# LocalControl

Tell your Windows PC how it should be used, in plain English, and a local model enforces it.

> "For the next hour, I can only use this laptop to read papers or search for papers or technical material."

Rules are **locked in**: once set, a rule cannot be stopped, replaced or quit out of until it expires (60 min if you do not give a duration, at most 12 h). Prompts you have used before are saved, counted and shown most-used-first, so a common rule is one click away.

Nothing leaves your machine. The rule and prompt history are stored locally, and the local model runs on your own computer.

## How it works

Every 1.5 s LocalControl looks at the foreground window, its title and, for browsers, the active tab's URL. It asks a local model (via [Ollama](https://ollama.com) or [llama.app](https://llama.app)) whether that fits the rule, then:

- **Browsers** (Chrome, Edge, Brave, Vivaldi, Opera, Firefox, LibreWolf, Waterfox, Zen): sends a disallowed tab to a local block page. The block page has an **appeal** box: explain why you need the page and the model reconsiders.
- **Other apps**: minimises the window and shows a notification.
- **Usage**: time per app and per website today (idle time excluded).

Each page or window is judged once and then cached. If the model server is down during a rule, LocalControl **fails closed**: anything not already judged is blocked, and it tries to restart the model server.

Chromium browsers are read through UI Automation. Firefox exposes its page URL through MSAA. Other apps are identified by their executable and window title.

## Requirements

- Windows 10 or 11
- Python 3.10+ (64-bit Python is required — the Win32 COM/INPUT bindings assume x64.)
- [uv](https://docs.astral.sh/uv/)
- A local model server: Ollama or llama.app

LocalControl screenshots the screen when the model needs an image to judge a
window (only with `--vision auto/always`, and only for the local model request).
Each capture is written to a unique `%USERPROFILE%\.localcontrol\screen-*.jpg`,
sent only to the local model server, and deleted as soon as the judgment
returns; any left behind by a crash are pruned after a day.

## Quick start

```powershell
uv run localcontrol serve          # dashboard at http://127.0.0.1:8765
```

Set a rule from the dashboard or the command line:

```powershell
uv run localcontrol cmd "for the next hour, only papers or technical material"
uv run localcontrol status
```

Pull a model for Ollama first:

```powershell
ollama pull gemma3:4b
```

LocalControl auto-detects Ollama on port 11434 or llama.app on port 9931. Pick one explicitly with `--backend ollama|llama`, and override the model or URLs with `--model`, `--ollama-url` and `--llama-url`. On llama.app it prefers a loaded text-generating model over decision-only or embedding models.

## Run at logon

```powershell
uv run localcontrol install        # run at logon under Task Scheduler, with a watchdog
uv run localcontrol uninstall      # refused while a rule is active
```

`install` creates two per-user Task Scheduler tasks:

- `LocalControl\Agent` runs the monitor and is restarted by Windows if it dies.
- `LocalControl\Guard` re-creates and re-runs the agent if its task is deleted, stopped, or disabled.

The monitor also re-creates the guard. Pristine XML copies live in `%USERPROFILE%\.localcontrol\tasks`, so deleted tasks are restored.

## Commands

```text
localcontrol serve [--backend auto|ollama|llama] [--model NAME] [--vision off|auto|always] [--dry-run]
localcontrol cmd "..." [--minutes N]
localcontrol history [N]
localcontrol status
localcontrol stop          # only once the rule has expired
localcontrol install
localcontrol uninstall
localcontrol guard         # watchdog loop (installed for you)
```

## How hard is it to get out of a rule?

It is designed as strong friction, not real security:

- Killing the monitor relaunches it through Task Scheduler.
- Stopping the model server blocks everything not already judged.
- The watchdog task recreates or restarts a killed monitor.
- Deleting both tasks or the Python environment still works, but the rule stays in `state.json` if the monitor is restarted before it expires.

Windows-level network filtering is not implemented, so blocking happens per foreground window or browser tab rather than at the network layer.

## Layout

```text
pyproject.toml
eval_judge.py            # checks the judge prompt on canned activities
localcontrol/
  __main__.py            # CLI: serve, cmd, status, install, ...
  controller.py          # policy state, judgments, enforcement, usage
  windows.py             # Win32, UI Automation and MSAA desktop integration
  winservice.py          # Task Scheduler tasks and watchdog
  activity.py            # current-activity model
  llm.py                 # Ollama and llama.app clients
  server.py              # local dashboard and JSON API
  static/                # dashboard and block page (served by server.py)
```

Data lives in `%USERPROFILE%\.localcontrol` (override with `LOCALCONTROL_HOME`). The service writes `server.log` and `guard.log` there. To see what LocalControl can read on your machine:

```powershell
uv run python -m localcontrol.windows
```
