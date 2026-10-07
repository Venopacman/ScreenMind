# Capture architecture gaps

From the session "Capture architecture map" (2026-10-07). The full map is [docs/architecture/capture.md](../architecture/capture.md). Gap ids (G4, G7...) match its section 8. G10, G11 and G13 were fixed on `custom` while the map was written (`fa80d39`, `81277a8`). Gaps that already have an item in another backlog file are linked there, not repeated here.

## Items

### G7: No a11y text or URL for displays without focus
Status: idea

Only the focused display gets a11y text and `active_url`. A browser on the second screen gets OCR and no URL. On macOS, `get_window_url(pid, bounds)` already reads one window's URL, and `_ax_window_at()` finds the AX window by bounds, so both could run per display. Windows would need the same per-HWND calls (`_window_documents(hwnd)` exists).

Refs: `CaptureWorker._capture_monitor()`, `_label_monitor()`, `MacOSAdapter.get_window_url()`.

### G4: A static screen gets no rows
Status: open (needs a decision)

The `run()` docstring says a capture is forced every interval even with no change. In the code the periodic grab goes through the same pHash dedup. Reading one page for 10 minutes gives one row. Time per page or app is then hard to measure. Options: keep a heartbeat row (no image, no analysis) per interval, or let the periodic grab skip dedup.

Refs: `CaptureWorker.run()`, `_capture_monitor()`, `capture/dedup.py`.

### G12: a11y text has no column of its own
Status: idea

a11y text is merged into `activities.ocr_text`, alone or above the OCR text, and only `analysis_method` tells which. Feeds and benchmarks can't compare the two sources. Since `81277a8`, skipped rows keep the a11y text in `ocr_text` too, and backfill reads it back from there.

Refs: `analysis_worker._process()` (step 3), `storage/database.py` migrations.

### G14: OCR reads one script per frame
Status: open

`_rec_model()` picks one recognizer. With `OCR_LANGUAGES=en,es,de,fr,ru` the Cyrillic model reads all Latin text. Check accented Latin (é, ü, ñ) on a live frame. If it is poor, run a second recognizer on the same boxes for Latin-heavy lines.

Refs: `screenmind/engine/ocr.py` (`_rec_model`, `_fix_lookalikes`).

### G16: No call room URL on Windows
Status: open

`WindowsAdapter` has no `get_window_url()`, so `meetings.url` is NULL for browser calls (Meet). Implement it with `_window_documents(hwnd)` + `pick_page_document()` for the HWND that matches the window bounds.

Refs: `screenmind/platform_support/windows.py`, `base.PlatformAdapter.get_window_url()`, `AudioWorker._read_url()`.

### G17: No mic-in-use signal on Windows
Status: open

`mic_apps()` returns `None` on Windows. Discord calls are never detected, Slack needs a "huddle" title, and a call whose window is hidden ends after 120 s. Windows lists apps using the mic in the registry (`HKCU\Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone\...`, `LastUsedTimeStop == 0` means in use). That needs no permission.

Refs: `base.PlatformAdapter.mic_apps()`, `workers/call_detection.py`.

### G18: macOS transcripts have the mic side only
Status: idea

`_find_loopback_device()` looks for an input named "loopback" or "stereo mix". macOS has none unless the user installs a virtual device (BlackHole names itself "BlackHole", so it is not found either). macOS 14.2+ has Core Audio process taps for system audio without a driver. Only matters when `MEETING_TRANSCRIPTION` is on.

Refs: `AudioWorker._find_loopback_device()`, `_recording_loop()`.

### G24: Retention runs only at startup
Status: open

`cleanup_old_data()` runs once in `main.py`. The main instance can run for weeks, and then nothing is deleted. Run it once a day from a worker loop too.

Refs: `screenmind/main.py:175`, `Database.cleanup_old_data()`.

### G25: Elevated windows on Windows
Status: idea (not tested)

A non-elevated process can't read UIA from, or get low-level input for, windows of elevated processes (UIPI). Admin consoles and installers are then missing a11y text and UI events. Check what is stored for an elevated PowerShell window.

Refs: `screenmind/platform_support/windows.py`, `capture/ui_events/windows.py`.

### G27: Linux has no UI events, browser URL or mic info
Status: idea

`create_backend()` returns `None` on Linux, and `LinuxAdapter` has no `get_browser_url()`, `list_visible_windows()` or `mic_apps()`. AT-SPI can give the URL (document attribute) and events. Low priority: nobody runs Linux here.

Refs: `screenmind/platform_support/linux.py`, `capture/ui_events/recorder.py` (`create_backend`).
