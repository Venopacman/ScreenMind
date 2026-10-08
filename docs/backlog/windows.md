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
Status: open (the user's step)

Since `c377a8c` (G39) the code defaults are the shared settings, and the dashboard stores only values that differ from them. Existing files still hold the defaults of the day they were written, and ScreenMind never rewrites them at startup. Clean them up once, with ScreenMind stopped (it writes `settings.json` on pause and resume), then start it and check the log line "Settings that differ from the defaults" in `~/.screenmind/screenmind.log`.

Windows `~/.screenmind/settings.json`, proposed whole file:

```json
{
  "setup_complete": true,
  "capture_paused": false
}
```

What that drops, by key:
- `capture_interval` 10 → default 40. Event-triggered capture already takes a frame on app switches, clicks and typing pauses; 40 s periodic means a quarter of the periodic grabs, OCR and analysis (lowest-resource principle).
- `ui_events_types` `click,app_switch,text,clipboard` → default `click,app_switch`. Typed text and clipboard are opt-in until there is PII detection (G31). If you want them, keep this key and put the same line in the Mac's file, so both machines record the same.
- `sensitive_filter_types` `credit_card,ssn,api_key,password` → default, which adds `jwt` (this file was missing it).
- Equal to today's defaults, no effect now but frozen against later changes: `performance_mode`, `context_window`, `kv_cache_quant`, `flash_attention`, `analysis_mode`, `auto_pause_heavy_apps`, `heavy_apps`, `defer_analysis`, `capture_active_monitor`, `meeting_transcription`, `meeting_apps`, `retention_days`, `sensitive_filter_enabled`, `encryption_enabled`, `ui_events_enabled`, `event_triggered_capture`, `active_model`, `model_variants` (`Q4_0` is the default variant).
- Keys of removed features, ignored since `cd95561`: `break_reminder_minutes`, `obsidian_*`, `notion_*`, `webhook_*`, `agents_*`, `auto_bookmark*`, `smart_notifications`, `distraction_minutes`, `dashboard_lock_timeout`, `*_hotkey`.
- Kept: `setup_complete` and `capture_paused` are state, not settings.

Mac: apply the same rules to `~/.screenmind/settings.json` (keep `setup_complete` and `capture_paused`, plus any key you chose on purpose and want on both machines). In the checkout's `.env`:
- `OCR_LANGUAGES`: drop it once the G32 default `en,es,de,fr,ru` is on `custom`.
- `CAPTURE_ON_START=true`: drop it. Both machines then restore the last capture state (`capture_paused`). The Windows laptop has never had it.
- Since `c377a8c` the `.env` is read from the checkout root, so a start at login (LaunchAgent, cwd `/`) now loads it too. Until now such a start ran without it.

Then run the e2e check on both machines: the status header row "Settings not at default" should match (empty, or the same keys).

## Fix now


### G37: One UIA client shared across threads
Status: open

`uiautomation`'s `_AutomationClient.instance()` is process-wide. Whichever thread calls UIA first (event loop, an executor thread or the UI-event enricher) creates it in its own COM apartment (`ComInit`, STA by default), and `set_uia_timeouts()` swaps in a `CUIAutomation8` on that thread. Other threads then use the raw pointer without marshaling. If the creating thread ends or stops pumping, calls can stall or fail. Suspected in the 2026-10-07 freeze (capture stopped after activity 175; shutdown hung). Since `d0ed3c2` (recorder lock) and `0d385bb` (5 s link timeout) a hang stops only UI events. Goal: a design that keeps capture, UI events and analysis up, and recovers by itself (a dedicated UIA thread, or one client per thread, plus a health check that restarts a stuck part).

Refs: `platform_support/windows.py` `_uia()`, `set_uia_timeouts()`, `capture/ui_events/windows.py`, `capture/ui_events/recorder.py`.

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
