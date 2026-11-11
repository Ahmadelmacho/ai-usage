# AI Usage

Windows tray app that shows how much of your Claude Code and Codex plan limits is left, as two numbers next to the clock.

Not made by or affiliated with Anthropic or OpenAI.
Claude and Codex logos are trademarks of Anthropic and OpenAI.
Logo images from [Lobe Icons](https://github.com/lobehub/lobe-icons) (MIT, see `usage_tray/logos/LICENSE`).

## Install and update

Paste this into PowerShell (no admin, no Python needed):

```powershell
irm https://raw.githubusercontent.com/Ahmadelmacho/ai-usage/main/get.ps1 | iex
```

The same command updates an installed copy. It downloads the latest release's `AIUsage.zip`, checks its SHA-256 against the one GitHub lists, and runs the installer inside, which stops the old app, swaps in the new one and starts it. A failed update puts the old app back and starts it. An update never adds back the start at sign-in if you removed it.

The app checks for a new release 1 minute after it starts and then every 6 hours. When one is out, the tray menu shows **Update to vX.Y.Z** and the window shows the same link by its title. Click it: a PowerShell window runs the command above, with the `get.ps1` of that release instead of `main`, and the app restarts on the new version.

To check now, choose **Check for updates** in the tray menu, or click the small **Check for updates** button by the window's title. It shows "Up to date" or "Couldn't check" for a few seconds, or the **Update to vX.Y.Z** link when one is out.

Releasing: set `__version__` in `usage_tray/__init__.py`, commit, then push the tag (`git tag v1.0.1; git push origin v1.0.1`). `.github/workflows/release.yml` tests, builds and publishes the release.

## What you need

- Windows 10 or 11. No other OS.
- Claude Code and/or Codex installed and signed in with a subscription login (Claude Pro/Max, ChatGPT plan). A login that uses only an API key has no plan limits, and the app shows "no limits for this login".
- A provider that is not installed shows "not found" (and `–` until it has numbers); the other one still works.

## What it shows

- **Tray:** two numbers next to the clock. Orange = Claude weekly % left. White = Codex 5-hour % left (or the weekly one, on a Codex plan without a 5-hour limit; the hover text names it). Amber badge at 25% or less, red at 10% or less, `–` before the first check.
- **Hover** a number: that provider's rows, reset countdowns and check time (Windows caps hover text at 127 characters).
- **Left-click** a number: open the window. **Right-click:** Update to vX.Y.Z (only when one is out) / Check for updates / Refresh / Open / Close. Close quits; the window's X only hides it.
- Checks every 5 seconds. A provider that fails backs off on its own (10 s, 20 s ... up to 5 minutes) and keeps showing its last good numbers. Those are kept in `%LOCALAPPDATA%\AIUsage\last.json`; errors go to `app.log` there.
- Maximize works: the window fills the screen with the cards in a centered column.

## What it reads, and what it never reads

- Reads `~/.claude.json` for two values only: the account id (`oauthAccount.accountUuid`) and Claude's saved usage snapshot (`cachedUsageUtilization`). Kept in memory, never logged. That file holds no tokens.
- For Codex it reads only the change time of `~/.codex/auth.json`, never its contents.
- It never opens `~/.claude/.credentials.json` and never reads or sends a token. Both CLIs refresh their own logins.
- It sends no prompt, so it uses no tokens and runs no tools.
- `app.log` may hold error text from the CLIs. Check it before you share it.

## How it reads the limits

Same method as [T3 Code](https://github.com/pingdotgg/t3code) (MIT), re-implemented here.

- **Claude:** runs `claude -p --input-format stream-json` with hooks off, no MCP servers, no user settings file and no session saved, then sends the control request `get_usage`. This request is not documented by Anthropic. It can change or go away in any Claude Code update, and the Claude number then stops. The direct `api.anthropic.com/api/oauth/usage` endpoint rate-limits (429), so it is not used.
- **How fresh Claude's numbers are:** Claude Code fetches `/api/oauth/usage`, which often answers 429 (too many requests); Claude then waits 5 min to an hour before asking again and answers from the snapshot it saved in `~/.claude.json` (`cachedUsageUtilization`, shared by every Claude session). The app reads that snapshot's time and, when the numbers are over 2 minutes old, says "numbers from 3:00 PM" in the hover text and in the window instead of "checked".
- **Codex:** runs `codex app-server` with plugins, apps and in-app updates off, and calls the documented JSON-RPC method `account/rateLimits/read`.
- Both CLIs stay running between checks, so a check is one message (about 0.06 s of CPU) instead of a fresh start (about 1.4 s). They restart when a check gets no answer, every 30 minutes, and when the signed-in account changes, since a running copy keeps the account it started with. Claude: the account id in `~/.claude.json`; its token file is rewritten whenever any Claude session refreshes a sign-in, so it can't tell a switch. Codex: the change time of `~/.codex/auth.json`.
- Both CLIs update plugins in the background when they start (dozens of git processes, and a restart cut big clones off so the next start began them again). Claude runs with `DISABLE_AUTOUPDATER=1` and no user settings file; Codex with plugins, apps and in-app updates off. Measured over 5 minutes: 7 processes throughout, no git, 1.1% of one CPU core.
- Each CLI starts suspended inside a kill-on-close Job Object, so a hang past 25 s, Close, or this app dying ends the CLI and everything it started.
- Memory: Claude copy about 265 MB, Codex copy about 130 MB, the app about 45 MB.

## Log (`%LOCALAPPDATA%\AIUsage\app.log`, 1 MB, one older file kept)

- `started` / `closed`: the app itself.
- `claude: started a copy (reason), pid N`: a hidden CLI copy started. Reasons: first check, every 30 min, after a failed check, the last one exited, the login changed (Claude: another account or signed out; Codex: also a token refresh, every few days). More than about 2 an hour per CLI means something is wrong.
- `codex check failed: ...` (warning) and `codex checks working again after N failed`.
- `health:` 1 minute after start, every 10 minutes, and on Close. Per CLI: checks ok and failed since the last line, average and slowest check time, copies and processes started so far (git storms show up here as a jump), processes running, CPU seconds so far, memory now. For the app: CPU so far, memory, handles, GDI and USER objects, threads. A number that keeps climbing across lines (memory, handles, GDI) is a leak.
- Unexpected errors carry a Python traceback.

## Build from source

Needs Python 3.13.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\pip install --require-hashes -r requirements.lock
.\build.ps1      # makes dist\AIUsage\AIUsage.exe (stops a running copy first)
.\install.ps1    # Start menu + startup shortcuts, starts it, shows both numbers on the taskbar once
```

`requirements.lock` pins every package with its hash (the release build installs only these). After changing `requirements.txt`, remake it with pip-tools on Windows and Python 3.13: `pip-compile --generate-hashes --allow-unsafe --strip-extras --no-emit-index-url --output-file=requirements.lock requirements.txt`.

Tests:

```powershell
.\.venv\Scripts\python -m pytest -q                       # unit, drawing and app tests
$env:LIVE=1; .\.venv\Scripts\python -m pytest -q          # also the live tests (real, signed-in claude and codex)
.\.venv\Scripts\python tests\preview.py out               # PNGs of the window and tray icons
```

`tests\capture_fixtures.py` saves real CLI answers to `tests\fixtures\local\` (ignored by git, since they show your plan and use).

To share a build: `.\build.ps1; .\pack.ps1` makes `dist\AIUsage.zip`. The other person extracts it and double-clicks `Install.cmd` (`share\install.ps1`): it copies the app to `%LOCALAPPDATA%\Programs\AIUsage`, adds the same shortcuts (the startup one on a first install only) and starts it. No admin, no Python.

Code: `usage_tray/limits.py` (CLI checks, parsing, text), `usage_tray/draw.py` (Pillow pictures), `usage_tray/app.py` (window, tray icons, timer). Logo: `usage_tray/logo.png`.

## Uninstall

1. Right-click a tray number and choose Close.
2. Delete `AI Usage.lnk` from the Start menu (`shell:programs`) and from `shell:startup`.
3. Delete `%LOCALAPPDATA%\Programs\AIUsage` (the app) and `%LOCALAPPDATA%\AIUsage` (saved numbers and log).

## License

MIT. See `LICENSE`.
