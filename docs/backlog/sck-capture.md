# ScreenCaptureKit capture on macOS

From the session "SCK capture" (2026-10-07).

Done on `custom`: `screenmind/capture/sck.py` grabs each display with `SCScreenshotManager`. `ScreenCapture._grab()` tries SCK, then the `screencapture` tool, then mss. After one SCK timeout (3 s) the process uses the tool for good. `scripts/sck-bench.py` times the backends per display.

What we know:

- From a process started by a Claude session, `SCShareableContent` answers in ~15 ms, but the `SCScreenshotManager` handler never fires (waited 45 s). `CGWindowListCreateImage` hangs 30 s there. `CGPreflightScreenCaptureAccess()` is True, so the grant is there. Claude.app starts its children through a `disclaimer` helper, so the Python process is its own "responsible process" for TCC. That is the likely cause. The `screencapture` tool works there in ~0.15 s.
- An abandoned SCK request does not block other apps the way a hung CG grab does. With one request given up after 3 s, the main instance kept its normal 5-6 s grab rhythm (checked with `/api/status` counters, 2026-10-07).
- So dev instances started from Claude only test the fallback path. Real SCK timings need a process started from Terminal.app, like the main instance.

## Items

### macOS "still recording your screen" reminder
Status: open

macOS 15+ can show a periodic reminder or ask again to allow screen recording for apps that capture without a picker. Nobody has seen it for ScreenMind yet. If it shows up, note how often and whether a grab blocks while it is open. A blocked grab hits the 3 s timeout and the process moves to the `screencapture` tool for good.

### SCK is dropped for the whole process after one timeout
Status: idea

After a timeout, `SCKGrabber.disabled` stays True until restart, because a stuck request may hold up other apps that capture. If timeouts turn out to be rare and short (sleep/wake, display reconfigure), retry SCK once after some minutes instead.

Refs: `SCKGrabber.grab()`, `_disable()` in `screenmind/capture/sck.py`.

### Leave windows out of screenshots
Status: idea

`SCContentFilter` can exclude windows. Use it to leave out ScreenMind's own dashboard tab, or the windows of blocked apps instead of skipping the whole display (`CaptureWorker._capture_tick()` skips the display today). Ask the user first.

### Find out why grabs hang in Claude-started processes
Status: open, low priority

Check whether the TCC "responsible process" theory holds, for example by granting Screen Recording to the uv Python binary (`~/.local/share/uv/python/cpython-3.12.13-macos-aarch64-none/bin/python3.12`) or to Claude.app's `claude` binary. If a grant fixes it, dev instances can test SCK for real.
