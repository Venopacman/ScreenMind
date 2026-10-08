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

## Fix now

### G32: OCR reads Latin only on Windows
Status: open

`OCR_LANGUAGES` defaults to `en`, and this laptop has no `.env`, so Cyrillic is read by the Latin model ("Работа" -> "Pa6ota", "GitHub — Википедия" -> "WGitHub —BukuneAna"; activities 183, 198, 2026-10-07). The Mac uses `en,es,de,fr,ru`. Pick a default that matches across machines (for example from the OS UI languages), within the one-recognizer-per-frame limit (G14).

Refs: `config.py` `ocr_languages`, `engine/ocr.py` `_rec_model()`, `.env.example`.

### G37: One UIA client shared across threads
Status: open

`uiautomation`'s `_AutomationClient.instance()` is process-wide. Whichever thread calls UIA first (event loop, an executor thread or the UI-event enricher) creates it in its own COM apartment (`ComInit`, STA by default), and `set_uia_timeouts()` swaps in a `CUIAutomation8` on that thread. Other threads then use the raw pointer without marshaling. If the creating thread ends or stops pumping, calls can stall or fail. Suspected in the 2026-10-07 freeze (capture stopped after activity 175; shutdown hung). Since `d0ed3c2` (recorder lock) and `0d385bb` (5 s link timeout) a hang stops only UI events. Goal: a design that keeps capture, UI events and analysis up, and recovers by itself (a dedicated UIA thread, or one client per thread, plus a health check that restarts a stuck part).

Refs: `platform_support/windows.py` `_uia()`, `set_uia_timeouts()`, `capture/ui_events/windows.py`, `capture/ui_events/recorder.py`.

### G38: No log file without a console
Status: open

`launcher.vbs` and `launcher.start_screenmind()` start `pythonw -m screenmind` with stdout and stderr to `DEVNULL`. `config._setup_logging()` writes `screenmind.log` only when `sys.stderr` is `None`, which is not the case with `DEVNULL`. So the usual start has no log. Diagnosing the 2026-10-07 freeze needed a manual restart with `SCREENMIND_LOG_FILE`.

Refs: `screenmind/config.py` `_setup_logging()`, `screenmind/launcher.py`, `~/.screenmind/launcher.vbs`, `startup.py`.

### G39: Machines run with different settings
Status: open

The Mac main instance loads a `.env` (OCR languages, capture on start). The Windows laptop has none, and its `settings.json` differs (`capture_interval` 10, `performance_mode` balanced, `ui_events_types` with text and clipboard). The user wants both machines on the same parameters, or at least the same meaning and results. Make the defaults the shared source, and keep per-machine overrides few and visible (for example in the status file header).

Refs: `config.py`, `.env.example`, `~/.screenmind/settings.json`, `scripts/e2e_collect.py` (header), `scripts/dev-instance.sh`.

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
