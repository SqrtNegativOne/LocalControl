# LocalControl

Tell your Mac and iPhone how they should be used, in plain English, and a local model enforces it.

> "For the next hour, I can only use this laptop to read papers or search for papers or technical material."

Rules are **locked in**: once set, a rule can't be stopped, replaced or quit out of until it expires (60 min if you don't give a duration, at most 12 h). A rule set on one device syncs to the other through iCloud. Prompts you've used before are saved, counted and shown most-used-first, so a common rule is one tap away.

Nothing leaves your devices except the rule and prompt history, which sync through your own iCloud account.

## Mac: menu-bar app

Every 1.5 s it looks at the frontmost app, its window title and, for browsers, the active tab's URL. It asks a local model (via [Ollama](https://ollama.com)) whether that fits the rule, then:

- **Browsers** (Chrome, Safari, Firefox, Arc, Brave, Edge, …): sends a disallowed tab to a local block page. The block page has an **appeal** box: explain why you need the page and the model reconsiders.
- **Other apps**: hides the app and posts a notification.
- **Usage**: time per app and per website today (idle time excluded).

Each page or window is judged once and then cached. Single-topic sites (e.g. arxiv.org) are cached for the whole domain, while mixed sites (YouTube, Google, Reddit) are judged per page. If Ollama is down during a rule, the app **fails closed**: anything not already judged is blocked, and it tries to restart Ollama.

```sh
brew install ollama xcodegen && brew services start ollama && ollama pull gemma3:4b
echo YOUR_TEAM_ID > .team   # Apple developer team ID used for signing (or export DEVELOPMENT_TEAM)
./build.sh --install        # builds, copies to /Applications, launches
```

To build under your own account, also replace the `com.eugenevinitsky` bundle prefix in `project.yml`, the entitlements, `Shared/Rule.swift`, `Mac/LocalControlApp.swift` and the agent plist.

On first launch:
- **Login Items**: the app registers a keep-alive agent (System Settings → General → Login Items → allow LocalControl). macOS relaunches it within seconds if it's killed or force-quit. A normal Quit is only offered when no rule is active.
- **Accessibility**: needed for window titles and Firefox URLs (System Settings → Privacy & Security → Accessibility).
- **Automation**: approve "LocalControl wants to control Google Chrome/Safari/System Events" when asked.
- **Screen Recording**: only if screenshots are on (Settings in the menu: Off / When needed / Always). "When needed" sends a screenshot to the model only when there's no URL or title to go on.

The model is configurable in the menu's Settings (any Ollama model; vision models can use screenshots).

`LocalControl.app/Contents/MacOS/LocalControl --selftest` observes the screen once and judges it, plus two sample pages, against a sample rule. It doesn't touch any state.

## iPhone app

iOS doesn't let apps watch the screen, so the phone works differently. When you set a rule, an on-device model (Qwen3 1.7B via MLX, ~1 GB, downloaded on first use) translates it into **Screen Time** settings, which you review before locking in:

- **Allowlist rules** ("only papers"): every app is shielded except the ones you chose as *always allowed*, and only the listed websites load.
- **Blocklist rules** ("no social media"): the apps and categories you chose as *distracting* are shielded, and the listed websites are blocked.

A Screen Time monitor extension lifts the restrictions when the rule expires, even if the app isn't running. A rule set on the Mac is translated and applied the next time you open the phone app.

```sh
./phone.sh          # build and install on a connected/paired iPhone
```

Uses Family Controls, so TestFlight/App Store distribution needs Apple's Family Controls distribution entitlement; dev installs work without it.

## How hard is it to get out of a rule?

It's designed as strong friction, not real security:
- **Mac**: killing the app relaunches it, and stopping Ollama blocks everything not already judged. Removing it from Login Items in System Settings, or editing its saved state, still works.
- **iPhone**: you can revoke LocalControl's Screen Time access in Settings, or delete the app.

## Layout

- `Mac/`: menu-bar app (observation, judging, enforcement, block-page server)
- `iOS/App`: iPhone app (MLX translation, Screen Time), `iOS/Monitor`: expiry extension
- `Shared/`: rule model, prompt history, iCloud sync, prompts
- `python/`: the original Python prototype (stdlib only; `python3 -m localcontrol serve`) and `eval_judge.py`, which checks the judge prompt on canned activities
