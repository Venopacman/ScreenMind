# Uptime: no stuck part stops ScreenMind

Status: steps 1-3 built on 2026-10-08 for G37 ([windows.md](../backlog/windows.md) "G37"). Steps 4-5 come later.

Goal: ScreenMind runs all day. A hung OS call must not stop capture, analysis or shutdown, and a part that stops anyway is noticed and restarted. All of this must stay cheap (see "lowest resource use" in `CLAUDE.md`): no extra process, no polling faster than every 30 s, and no work when nothing is wrong.

## What happened on 2026-10-07

The main instance on Windows stored activity 175 and then stopped: `captures` stayed at 0, no UI events were stored, and `/api/shutdown` hung. `captures` is counted after UI events are linked, so capture was stuck in `_link_ui_events()`. That waited on the recorder lock, which the enricher thread held during a UIA call. `stop()` waited on the same lock without a timeout, so shutdown hung. `d0ed3c2` and `0d385bb` cut that chain, but they don't explain why the UIA call took minutes.

## Findings (2026-10-08, Windows 11, `uiautomation` 2.0, Python 3.14)

Scenarios are in `scripts/uia_threads.py`.

1. **The shared client across threads is ruled out as the cause.** `CUIAutomation` and `CUIAutomation8` are registered `ThreadingModel=Both`. So the client is made in the calling thread's apartment and used through a raw pointer. Elements are `IAgileObject`; the client is not. These cases all worked, with no error and no delay:
   - the thread that created the client exits, then other threads use it (`exit`, `exit2`);
   - the asyncio thread (STA, never pumps messages) creates it, and a worker uses it while that thread sleeps (`nopump`);
   - the event loop and a worker use it at the same time, 638 rounds in 30 s (`both`).

   Still untidy: `import comtypes` calls `CoInitializeEx` on whichever thread imports it first and never releases it.
2. **The per-call timeout works, but only if `set_uia_timeouts()` worked.** On a hung window (a Tk app that stopped pumping), each call fails after 1.0 s. Without the timeouts, `_window_documents()` and `extract_a11y_text()` block for over 60 s: UIA's default is 20 s per call, and a read makes many calls. If `set_uia_timeouts()` failed, it logged only at debug level and was never retried. The whole process then ran without timeouts, and nobody saw it.
3. **No overall time limit on UIA reads.** One capture-time read is many UIA calls:
   - the native walk goes up to depth 8 and 500 lines, with several calls per node;
   - `_window_documents()` walks up to 40 parents for each Document, and Firefox lists the Documents of background tabs too.

   Each call can take up to 1 s, so an app that answers slowly (busy, but not yet marked hung) can hold one read for minutes. The enricher's `element_at()`, `focused_element()` and `browser_url()` work the same way.
4. **Capture-time UIA runs on the event loop.** `get_active_window_title()`, `get_top_window_in()` (both through `_best_title()`) and `_read_focused()` call UIA synchronously in `CaptureWorker._capture_tick()`. While one of them is slow, the whole loop waits. There's no capture, no analysis and no shutdown, because the shutdown request is scheduled on the loop too.
5. **Shutdown waits for stuck executor threads.** A `run_in_executor` job that never returns (for example a timed-out `_link_ui_events()`) blocks exit. `asyncio.run()` waits 300 s for the default executor. Then `concurrent.futures` joins its threads at exit with no timeout, so the process never ends.
6. **Clipboard reads can block forever.** `GetClipboardData` asks the clipboard owner to render delayed formats and waits without a timeout. This only runs when the `clipboard` UI event type is on.

Most likely sequence on 2026-10-07: a slow app made one recorder UIA read take minutes (3). The enricher held the lock during that read, and capture and shutdown waited on the lock. The cross-thread suspect (1) didn't reproduce.

## Plan

### 1. UIA worker threads with deadlines (built)

`UiaWorker` in `platform_support/windows.py`: one daemon thread that owns COM and runs whole UIA jobs ("read the a11y text of this window"), not single calls. There are two workers, so neither consumer waits behind the other's slow read:

- `capture`: a11y text (3 s), page URL and Document title (1.5 s each);
- `recorder`: clicked element, focused element and page URL (1.5 s each).

A third worker with the same rules reads the clipboard (finding 6). It starts only when the `clipboard` type is on.

- **Per-job deadline.** The caller waits at most the job's timeout, then gets `None` and goes on: OCR covers the text, and the title stays the window title.
- **Deadline inside walks.** `_walk_tree()`, `_window_documents()` and the page loop check `uia_out_of_time()` between nodes. The walk stops early enough for its last call (1 s at most) to end in time, and returns what it has read so far. Partial text is noted in the debug log only. The stored text gets no marker.
- **A stuck job doesn't make others queue.** A job that is slow but still within its deadline makes the next one wait. A job past its deadline makes new jobs fail at once.
- **Stuck threads are replaced, with a cap.** After 30 s the thread is left behind and a new one takes over, with a warning that names the job. A Python thread stuck in COM can't be killed, so each replacement leaks one thread. After 3 replacements in an hour, UIA is off for that consumer until restart, with one error in the log.
- **Timeouts are checked.** Each new worker thread sets the 1 s UIA timeouts. A failure is now a warning, and the next thread tries again.
- **COM only on the worker threads.** `_ensure_a11y_init()` no longer imports `uiautomation` (and with it comtypes) on the event loop. Elements never leave their worker thread, so the cross-thread question (finding 1) goes away.
- **Cost:** two idle threads, and a third only with clipboard events on. No polling.

### 2. Clean shutdown under a stuck thread (built)

`watchdog.start_shutdown_deadline()`: when a stop is asked for (Ctrl+C or `/api/shutdown`), a 45 s daemon timer starts. A normal stop takes up to about 25 s (recorder 7 s, call recording 5 s, llama-server 10 s).

If the process is still alive when the timer fires, it:

1. logs the stacks of all threads;
2. runs `PRAGMA wal_checkpoint(PASSIVE)` on its own connection;
3. flushes the logs;
4. logs a warning and ends with `os._exit(3)` (`FORCED_EXIT_CODE`).

A normal stop exits with 0, so the e2e check and the logs can tell a forced stop from a clean one. A supervisor must not restart on either code: both are stops the user asked for. This covers stuck executor threads (finding 5) and a stuck event loop. `/api/shutdown` starts the timer from the uvicorn thread, so a blocked event loop can't delay it. A normal stop ends before the timer, which dies with the process.

### 3. Health watchdog (built)

`watchdog.Watchdog` is a daemon thread started in `main()`. It wakes every 30 s and reads heartbeats that the parts keep anyway. It costs nothing between wakes. A heartbeat must mean stuck, not slow or idle.

| Part | Heartbeat | Stuck when | Action |
|---|---|---|---|
| Capture loop | `CaptureWorker.last_beat`, each loop pass (at most 5 s apart, also while paused or idle) | no pass for 2 min | log the stacks of all threads; again every 10 min while it lasts |
| UI-event enricher | `UiEventRecorder.ticks`, each tick | count unchanged for 2 min while running | log stacks, then `restart()`: a new enricher with a new lock, the stuck thread left behind. At most 3 times per hour |
| UIA workers | job start time | one job over 30 s | replaced by the worker itself (step 1) |
| Analysis | `AnalysisWorker.last_beat`, each loop pass | frames waiting and no pass for 20 min | log stacks (one Gemma call may take 300 s, with retries) |

The stack dump is the main tool: if a freeze happens again, the log shows where each thread waits. Since G38 the log file is on by default, so the cause is no longer a guess.

### 4. Restart a stuck capture loop (later)

The capture loop shares the event loop with analysis. Once steps 1-3 have run for a while, check the logs. If the capture loop still gets stuck, move `_capture_tick()` off the loop to its own thread, like the recorder. The watchdog can then replace it the way it replaces the enricher.

### 5. Process supervisor (later, with packaging)

The last resort is a process restart: the watchdog exits with a non-zero code after the capture loop has been stuck for 15 min. That only helps once something restarts the process. The shipped agent ([packaging.md](packaging.md)) gets one:

- macOS: launchd restarts a login item that exits with an error (`KeepAlive` with `SuccessfulExit=false`).
- Windows: the tray item is the parent process and restarts the agent, at most 3 times in 10 min.

Until then, the dev setup has no restarter, so the watchdog only logs.

## Tests

`tests/test_uptime.py` uses fake jobs that hang on an `Event`. It runs on any OS and checks that:

- a UIA job past its deadline returns `None` in time, and the next job fails fast instead of waiting;
- a job behind one that is still on time waits for it and runs;
- a stuck thread is replaced, and after the cap UIA is off for that consumer;
- a walk over a slow app stops at its deadline with the text it has read so far;
- the adapter (a11y text, Document title), the backend (`element_at`, `focused_element`) and the clipboard read give up on a hung app, and capture and recorder don't wait for each other;
- the watchdog sees a capture loop, enricher or analysis with no heartbeat, but not a paused, idle or stopped one; it restarts the recorder, with a cap;
- a recorder restart with an enricher stuck holding the lock gets a new lock, and capture doesn't wait on the old one;
- the shutdown deadline ends a process that a stuck non-daemon thread keeps alive, with exit code 3, and doesn't delay a normal exit (code 0).

Live check: `scripts/uia_threads.py hung` against a non-pumping Tk window. Results: a11y text 1.3 s (limit 3 s), Document title 1.0 s and `element_at` 1.0 s (limit 1.5 s).
