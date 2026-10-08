# Windows laptop

From the session "Win data quality" (2026-10-07/08) on the Windows 11 laptop (RTX 3070 Laptop 8 GB, 16 GB RAM, one display). Gap ids match [capture.md section 8](../architecture/capture.md#8-gaps). Decisions are the user's, from 2026-10-08.

## Principle: lowest resource use

The long-term aim is to use as few resources as possible (CPU, RAM, GPU, battery). So items that would make ScreenMind heavier, such as tuning Gemma per frame onto the GPU, are parked rather than done. See also the resource budget in [packaging.md](../plans/packaging.md).

## State of this machine

### Status file is from before the strip
Status: open

[docs/status/windows.md](../status/windows.md) is a hands-free run (`--no-input`) from a Claude session on `49f56cd`, before the strip-to-actions slices: 14 PASS, 0 FAIL, 5 SKIP. The SKIPs are click, typed text, typed-secret redaction, password field (they need typing by hand on Windows) and calls. Run it again by hand on current `custom`, from the user's own PowerShell: `uv run python scripts\e2e_collect.py --write-status --browsers chrome,msedge,firefox`. `f3ee4a3` lets the analysis wait finish once the person takes the computer back.

### Main instance on old code
Status: open

On 2026-10-08 the main instance still ran `0cac8fa` on the old pip `.venv` (Python 3.12). It needs `git pull`, `uv sync` (from the user's own terminal, see "Claude sessions and AppData" below) and a restart, which runs migration v11. A backup made with SQLite's backup API is at `~/.screenmind/screenmind.db.pre-v11` (schema v10, 495 activities, integrity ok).

### Settings cleanup (both machines)
Status: Windows done 2026-10-08 (11:51 start logs only `capture_interval` and `ui_events_types`, both code defaults since `8313d26`/`5c419f8`, so they go away once the main instance runs that code); Mac open

Since `c377a8c` (G39) the code defaults are the shared settings, and the dashboard stores only values that differ from them. Existing files still hold the defaults of the day they were written, and ScreenMind never rewrites them at startup. Clean them up once, with ScreenMind stopped (it writes `settings.json` on pause and resume), then start it and check the log line "Settings that differ from the defaults" in `~/.screenmind/screenmind.log`.

The user's decisions of 2026-10-08, per key:
- `capture_interval: 10`: the code default since 2026-10-08, so the key has no effect once the main instance runs that code, and can go then.
- `ui_events_types: "click,app_switch,text,clipboard"`: the code default since 2026-10-08 (the user's call on G31), so this key too can go once the main instance runs that code. No Mac step. Typed text and clipboard stay on, with the sensitive-data filter and password redaction. G31 stays open.
- `performance_mode`: Windows follows the default (`balanced`). The Mac keeps `maximum` on purpose: with `balanced`, Apple Silicon ran Gemma on about 4 CPU cores and took 10.5 s per frame against 6.2 s on the GPU. This is the one key where the machines differ.

Windows `~/.screenmind/settings.json`, whole file:

```json
{
  "setup_complete": true,
  "capture_paused": false,
  "capture_interval": 10,
  "ui_events_types": "click,app_switch,text,clipboard"
}
```

What that drops, by key:
- `sensitive_filter_types` `credit_card,ssn,api_key,password` → default, which adds `jwt` (this file was missing it).
- Equal to today's defaults, no effect now but frozen against later changes: `performance_mode`, `context_window`, `kv_cache_quant`, `flash_attention`, `analysis_mode`, `auto_pause_heavy_apps`, `heavy_apps`, `defer_analysis`, `capture_active_monitor`, `meeting_transcription`, `meeting_apps`, `retention_days`, `sensitive_filter_enabled`, `encryption_enabled`, `ui_events_enabled`, `event_triggered_capture`, `active_model`, `model_variants` (`Q4_0` is the default variant).
- Keys of removed features, ignored since `cd95561`: `break_reminder_minutes`, `obsidian_*`, `notion_*`, `webhook_*`, `agents_*`, `auto_bookmark*`, `smart_notifications`, `distraction_minutes`, `dashboard_lock_timeout`, `*_hotkey`.
- Kept: `setup_complete` and `capture_paused` are state, not settings.

Mac `~/.screenmind/settings.json` gets the same file without `setup_complete` (the Mac file never had it). It keeps `performance_mode: maximum` and drops `capture_active_monitor: true` (a leftover from a 2026-10-06 test), `model_variants` and the old `sensitive_filter_types`. In the checkout's `.env`:
- `OCR_LANGUAGES`: drop it once the G32 default `en,es,de,fr,ru` is on `custom`.
- `CAPTURE_ON_START=true`: drop it. Both machines then restore the last capture state (`capture_paused`). The Windows laptop has never had it.
- Since `c377a8c` the `.env` is read from the checkout root, so a start at login (LaunchAgent, cwd `/`) now loads it too. Until now such a start ran without it.

Then run the e2e check on both machines: the status header row "Settings not at default" should match (empty, or the same keys).

## Fix now

### G37: No overall time limit on UIA reads
Status: fixed 2026-10-08 (steps 1-3 of [uptime.md](../plans/uptime.md)); steps 4-5 open

The first suspect, one `uiautomation` client shared across threads and COM apartments, was ruled out with repros (`scripts/uia_threads.py`): the client is `ThreadingModel=Both` and works from any thread, also after its creator exits or while it never pumps. The real gap was that UIA's 1 s timeout bounds one call, but a read is hundreds of calls. A slow app could hold a read for minutes:

- on the event loop (capture, analysis and the shutdown request all waited);
- or in the enricher while it held the recorder lock (the 2026-10-07 freeze: capture stopped after activity 175 and shutdown hung).

`set_uia_timeouts()` could also fail silently. A stuck executor thread kept the process alive after shutdown, and `GetClipboardData` has no timeout.

What changed:

- **UIA worker threads.** Capture, recorder and clipboard each have one, and every read has a deadline (3 s for a11y text, 1.5 s for lookups). Walks stop at the deadline and keep what they read. A stuck thread is replaced, at most 3 times an hour.
- **Watchdog.** It logs all thread stacks when the capture loop, the enricher or analysis stalls, and restarts a stuck enricher.
- **Shutdown deadline.** A stop that takes over 45 s is forced with exit code 3.

Open: moving capture off the event loop if the logs still show stalls (step 4), and a process supervisor with packaging (step 5).

Refs: `platform_support/windows.py` `UiaWorker`, `run_uia()`; `screenmind/watchdog.py`; `capture/ui_events/windows.py`; `capture/ui_events/recorder.py` `restart()`; [uptime.md](../plans/uptime.md).

### dev-instance.sh crashes on Windows without a console
Status: fixed 2026-10-08 (`c5e2598`)

Started with stdout redirected (from a Claude session, a scheduler, a pipe), `setup_llama.ensure_llama_server()` prints box-drawing characters to a cp1252 stdout and dies with `UnicodeEncodeError`. When stdin looks like a TTY, it offers to download llama-server instead, which a dev instance must never do (it shares the main one, `LLAMA_SERVER_SHARED`). Workaround (G37 session, 2026-10-08): `PYTHONIOENCODING=utf-8 ... scripts/dev-instance.sh </dev/null`. Fix: plain ASCII or a UTF-8-safe writer for that output, and no interactive prompt when `llama_server_shared` is set.

Fixed: with `llama_server_shared`, `ensure_llama_server()` only logs and returns: no lookup, prompt or download, and `main()` adopts the shared server (before, a dev instance logged "Starting without Gemma 4" with it up). `setup_llama` prints ASCII, and `run()` sets stdout to `errors="backslashreplace"`. Live: `scripts/dev-instance.sh </dev/null > dev.log 2>&1` without `PYTHONIOENCODING` starts and analyzes. The cause of the prompt: a stdin of NUL (`</dev/null`) is a TTY to `isatty()` on Windows.

Refs: `screenmind/setup_llama.py` `ensure_llama_server()`, `scripts/dev-instance.sh`, `73285fa`.

## Parked

### G33: Gemma runs mostly on the CPU here
Status: idea (parked, 2026-10-08)

`performance_mode=balanced` gives `-ngl 15`. On this laptop: ~26 s per new frame, ~8 cores busy, `llama-server` 2.5 GB resident and 7.5 GB committed, and about half the frames skipped as backlog while active. `maximum` (`-ngl 99`) would probably fit in 8 GB, but parked under the lowest-resource principle; per-frame Gemma is going to be reworked anyway.

Refs: `config.py` `num_gpu_layers`, `engine/model_manager.py`.

### G34: In-page buttons in Electron a11y text
Status: idea (parked, 2026-10-08)

The Claude desktop app now gives its conversation (about 3,000 chars per frame), but buttons inside the web Document are kept ("Copy", "Fork from here", "Hide sidebar"). It is LLM scope: no filter will cover every app.

Refs: `platform_support/windows.py` web Document walk.

### G35: Windows shell surfaces saved as activities
Status: idea (parked, 2026-10-08)

Task View, Task Switching, Start, Search and notifications get their own rows, with a11y text like "Task Switching | DesktopWindowXamlSource" and Gemma describing the window behind them. Come back if it skews data.

### Claude sessions and AppData
Status: idea (parked, 2026-10-08)

The Claude desktop app is an MSIX package. Files that Claude's shells write under `%APPDATA%` or `%LOCALAPPDATA%` are virtualized into `AppData\Local\Packages\Claude_<id>\LocalCache\...`, invisible to the user's own terminals. uv-managed Python installed from a Claude session lands there, so a `.venv` built on it fails in the user's terminal ("did not find executable"), and uv's own link check fails. Workaround: set `UV_PYTHON_INSTALL_DIR` and `UV_CACHE_DIR` outside AppData (for example `%USERPROFILE%\.local\share\uv\...`).

### Not tested on Windows
Status: idea (parked, 2026-10-08)

Chrome on a very large page (UIA cached fetch over the 1 s timeout should fall back to OCR), several displays (this laptop has one), calls (G16, G17; Discord is out of scope for now), elevated windows (G25, not a priority), typed-text redaction live (unit-tested, and checked against the leaked rows), and a Windows packaging spike (PyInstaller + Inno Setup, [packaging.md](../plans/packaging.md)).
