# AGENTS.md

## Project

LocalControl is a Windows-only Python desktop app. It watches the foreground
window and browser tab, asks a local model (Ollama or llama.app) whether the
activity fits a plain-English rule, and enforces the answer. Rules are locked
in until they expire.

The macOS, iOS, network-filter, and shared Swift code were removed. Do not add
Apple platform code back.

## Layout

```text
pyproject.toml
eval_judge.py          # offline check of the judge prompt on canned activities
tests/                 # stdlib unittest suite for the pure and platform-neutral pieces
localcontrol/
  __main__.py          # CLI: serve, cmd, history, status, stop, install, uninstall, guard
  controller.py        # policy state, judgments, enforcement, usage, main loop
  desktop.py           # the Windows backend seam imported by the controller
  windows.py           # Win32, UI Automation, MSAA, notifications, screenshots
  winservice.py        # Task Scheduler install/uninstall and the watchdog
  activity.py          # current-activity model and browser URL normalization
  llm.py               # Ollama and llama.app clients, backend auto-selection
  server.py            # local dashboard and JSON API
  static/              # dashboard and block page, served by server.py
```

`static/` stays inside the package: `server.py` serves it and `pyproject.toml`
ships it as package data.

## Tooling

- Use `uv`, not `pip`. Run commands from the repo root.
- Tests: `uv run python -m unittest discover -s tests -t .`
- Lint: `uvx ruff check .`
- Compile check: `uv run python -m compileall -q localcontrol eval_judge.py`
- Help/smoke test: `uv run localcontrol --help`
- Desktop probe (foreground app, browser URL, screenshot): `uv run python -m localcontrol.windows`
- Judge evaluation: `uv run python eval_judge.py`
- Serve locally: `uv run localcontrol serve --dry-run --no-open`

The app has no runtime dependencies beyond the Python standard library and
Windows APIs.

## Conventions

- Keep the controller platform-neutral; put OS calls in `windows.py` and
  expose them through `desktop.py`.
- Prefer type hints and docstrings. Match the surrounding style.
- The monitor loop must not crash on a failed probe or judgment. Fail closed
  when the model is unavailable.
- Do not log rules, prompts, or activity to remote services.
- Runtime data lives in `%USERPROFILE%\.localcontrol`; override with
  `LOCALCONTROL_HOME`.
- Do not commit unless the user asks.
