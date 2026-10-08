# ScreenCaptureKit capture on macOS

From the session "SCK capture" (2026-10-07).

Done on `custom`: `screenmind/capture/sck.py` grabs each display with `SCScreenshotManager`. `ScreenCapture._grab()` tries SCK, then the `screencapture` tool, then mss. After one SCK timeout (3 s) the process uses the tool for a cool-down, then probes SCK again (see "SCK comes back after a fallback" below). `scripts/sck-bench.py` times the backends per display.

Live: the main instance (started from Terminal.app, restarted 14:06 on `81bb786`) logs `Screen grab backend: sck` and saves a frame every ~10 s. From Terminal.app, an SCK grab takes ~43 ms against ~250 ms for the `screencapture` tool. Colors match (mean diff 1.19 per channel).

What we know:

- From a process started by a Claude session, the first `SCShareableContent` call answers in ~15 ms, but the `SCScreenshotManager` handler never fires (waited 45 s). Later `SCShareableContent` calls in the same process never answer either (2026-10-08), even the second one with no screenshot request in between. So there only the first SCK call of the process answers. `CGWindowListCreateImage` hangs 30 s there. `CGPreflightScreenCaptureAccess()` is True, so the grant is there. Claude.app starts its children through a `disclaimer` helper, so the Python process is its own "responsible process" for TCC. That is the likely cause. The `screencapture` tool works there in ~0.15 s.
- An abandoned SCK request does not block other apps the way a hung CG grab does. With one request given up after 3 s, the main instance kept its normal 5-6 s grab rhythm (checked with `/api/status` counters, 2026-10-07).
- So dev instances started from Claude only test the fallback path. Real SCK timings need a process started from Terminal.app, like the main instance.

## Items

### SCK timed out once in the bench while the main instance ran
Status: open, low priority

The first bench run from Terminal.app (`scripts/sck-bench.py`) timed out after 3 s on display 1 while the main instance was running. At that time the main instance still used mss (CoreGraphics), not SCK. The next run, with the main instance stopped, worked on the first try. Possible causes: contention with the CG grabs, a cold first request, or a TCC prompt waiting for an answer. Run the bench again while the main instance runs on SCK. If it times out again, the cause is contention between capture clients. That only affects ad-hoc tools, because the main instance falls back to the `screencapture` tool by itself.


### macOS "still recording your screen" reminder
Status: open

macOS 15+ can show a periodic reminder or ask again to allow screen recording for apps that capture without a picker. Nobody has seen it for ScreenMind yet. If it shows up, note how often and whether a grab blocks while it is open. A blocked grab hits the 3 s timeout and the process uses the `screencapture` tool until a probe works.

### SCK comes back after a fallback
Status: done 2026-10-08, waiting for a live check on the main instance

Before: one SCK timeout made the process use the `screencapture` tool until restart. On 2026-10-07 the main instance used SCK from 18:09:38, one request timed out at 18:10:50, and it stayed on the tool (~250 ms per grab instead of ~43 ms).

Now (`SCKGrabber` in `screenmind/capture/sck.py`):

- A timeout, or 3 errors in a row, start a 5 min cool-down. Grabs use the tool.
- Then one probe: a screenshot request that the capture tick does not wait for. It reuses the cached display, so it makes no blocking call. The next tick reads the answer. An answer within 3 s brings SCK back.
- A failed probe doubles the wait: 5, 10, 20, 40, then every 60 min.
- The back-off starts over once SCK has worked for 30 min after coming back. If it fails sooner, the next wait is longer.
- At most one SCK request is waited for at a time. A late answer drops its image at once.
- If SCK never gave a frame in the process (a Claude-started process), it stops for good after 2 failed probes. Why: such a process never gets an answer, and each lost request stays in memory. The probes cost no stall, so this only limits lost requests to 3 per process.

Memory: a request that never answers holds no thread and a few KB. 200 of them added about 4 MB (2026-10-08, from a Claude session).

Log lines: `ScreenCaptureKit timed out (...); using the screencapture tool, next try in 5 min` (WARNING), `ScreenCaptureKit probe ...; ... next try in N min` (INFO), `ScreenCaptureKit answered a probe in 0.05s; using it again` (INFO), then `Screen grab backend: sck`. `/api/status` has `capture.grab.backend` and `capture.grab.sck` (`state`, `next_retry`, `probing`, `lost_requests`).

Live check from a Claude session (2026-10-08): one 3 s stall at start, the 2 probes cost nothing, then `SCK never worked in this process, so using the screencapture tool from now on`. Still to check: the main instance after a real timeout. Look for the "answered a probe" line after a "timed out" line.

Refs: `SCKGrabber.grab()`, `_probe_says_back()`, `_send_probe()`, `status()` in `screenmind/capture/sck.py`; `ScreenCapture.grab_status()` in `screenmind/capture/screen.py`.

### Leave windows out of screenshots
Status: idea

`SCContentFilter` can exclude windows. Use it to leave out ScreenMind's own dashboard tab, or the windows of blocked apps instead of skipping the whole display (`CaptureWorker._capture_tick()` skips the display today). Ask the user first.

### Find out why grabs hang in Claude-started processes
Status: open, low priority

Check whether the TCC "responsible process" theory holds, for example by granting Screen Recording to the uv Python binary (`~/.local/share/uv/python/cpython-3.12.13-macos-aarch64-none/bin/python3.12`) or to Claude.app's `claude` binary. If a grant fixes it, dev instances can test SCK for real.
