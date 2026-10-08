"""
Watchdog: notice a stuck part of ScreenMind, and never hang on shutdown.

A daemon thread wakes every 30 s and reads heartbeats that the parts keep
anyway. It costs nothing between wakes and does nothing while all is well.

- Capture loop (`CaptureWorker.last_beat`, set on each pass of its loop,
  also while paused or idle): no pass for 2 min means the event loop or a
  capture call is stuck. Logs the stacks of all threads.
- UI-event enricher (`UiEventRecorder.ticks`): no tick for 2 min while it
  runs. Logs stacks and restarts the recorder, at most 3 times per hour.
- Analysis (`AnalysisWorker.last_beat`, set on each loop pass): frames
  waiting and no pass for 20 min. Logs stacks. One Gemma call may take
  300 s, with retries.

The stack dump says where each thread waits, so a freeze can be diagnosed
from the log (G37). See docs/plans/uptime.md.

The shutdown deadline (start_shutdown_deadline) ends the process if a
stuck thread keeps it alive after a stop was asked for.
"""

import logging
import os
import sys
import threading
import time
import traceback
from typing import Callable, List, Optional

logger = logging.getLogger("screenmind.watchdog")

CHECK_EVERY_S = 30.0
CAPTURE_STUCK_S = 120.0
RECORDER_STUCK_S = 120.0
ANALYSIS_STUCK_S = 1200.0
# Log the stacks of a part that is still stuck again after this long.
REPORT_AGAIN_S = 600.0
RECORDER_MAX_RESTARTS = 3
_RESTART_WINDOW_S = 3600.0
# A normal stop takes up to ~25 s (recorder 7 s, call recording 5 s,
# llama-server 10 s).
SHUTDOWN_DEADLINE_S = 45.0
# Exit code of a stop that had to be forced. A normal stop exits with 0. A
# supervisor must not restart on either: both are stops the user asked for.
FORCED_EXIT_CODE = 3


def thread_stacks() -> str:
    """The current stack of every thread, named, for the log."""
    names = {t.ident: t.name for t in threading.enumerate()}
    parts = []
    for ident, frame in sys._current_frames().items():
        stack = "".join(traceback.format_stack(frame))
        parts.append(f"--- thread {names.get(ident, '?')} ({ident}):\n{stack}")
    return "\n".join(parts)


class Watchdog:
    """Checks heartbeats of the capture loop, the UI-event recorder and the
    analysis worker. check() does one round and is what tests call."""

    def __init__(self, capture_worker=None, analysis_worker=None, ui_recorder=None,
                 analysis_queue=None, clock: Callable[[], float] = time.monotonic):
        self._capture = capture_worker
        self._analysis = analysis_worker
        self._recorder = ui_recorder
        self._queue = analysis_queue
        self._clock = clock
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._reported: dict = {}  # part -> time its stall was last logged
        self._recorder_ticks = (None, 0.0)  # (ticks, time they last changed)
        self._recorder_restarts: list = []
        self.recorder_restarts_off = False

    def start(self):
        self._thread = threading.Thread(target=self._run, name="watchdog", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.wait(CHECK_EVERY_S):
            try:
                self.check()
            except Exception as e:
                logger.debug(f"Watchdog check failed: {e!r}")

    def check(self) -> List[str]:
        """One round. Returns the parts found stuck (for tests and logs)."""
        now = self._clock()
        stuck = []
        if self._capture_stuck(now):
            stuck.append("capture")
        if self._recorder_stuck(now):
            stuck.append("recorder")
        if self._analysis_stuck(now):
            stuck.append("analysis")
        for part in ("capture", "recorder", "analysis"):
            if part not in stuck:
                self._reported.pop(part, None)
        return stuck

    # ── Parts ────────────────────────────────────────────────────────

    def _capture_stuck(self, now: float) -> bool:
        beat = getattr(self._capture, "last_beat", None)
        if beat is None or now - beat < CAPTURE_STUCK_S:
            return False
        self._report("capture", now, f"The capture loop has not run for {now - beat:.0f}s "
                     "(the event loop or a capture call is stuck)")
        return True

    def _recorder_stuck(self, now: float) -> bool:
        rec = self._recorder
        if rec is None or not getattr(rec, "running", False):
            self._recorder_ticks = (None, now)
            return False
        ticks = rec.ticks
        last, since = self._recorder_ticks
        if ticks != last:
            self._recorder_ticks = (ticks, now)
            return False
        if now - since < RECORDER_STUCK_S:
            return False
        self._report("recorder", now, f"The UI-event enricher has not ticked for {now - since:.0f}s")
        self._restart_recorder(now)
        return True

    def _restart_recorder(self, now: float):
        if self.recorder_restarts_off:
            return
        self._recorder_restarts = [t for t in self._recorder_restarts if now - t < _RESTART_WINDOW_S]
        if len(self._recorder_restarts) >= RECORDER_MAX_RESTARTS:
            self.recorder_restarts_off = True
            logger.error(f"UI events: the enricher got stuck {RECORDER_MAX_RESTARTS + 1} times "
                         "within an hour; not restarting it again. Restart ScreenMind.")
            return
        self._recorder_restarts.append(now)
        logger.warning("UI events: restarting the recorder (the stuck thread is left behind)")
        try:
            self._recorder.restart()
        except Exception as e:
            logger.error(f"UI events: restart failed: {e!r}")
        self._recorder_ticks = (self._recorder.ticks, now)  # the new enricher has 2 min

    def _analysis_stuck(self, now: float) -> bool:
        beat = getattr(self._analysis, "last_beat", None)
        waiting = self._queue.qsize() if self._queue is not None else 0
        if beat is None or not waiting or now - beat < ANALYSIS_STUCK_S:
            return False
        self._report("analysis", now, f"Analysis has not finished a frame for {now - beat:.0f}s "
                     f"with {waiting} waiting")
        return True

    def _report(self, part: str, now: float, message: str):
        """Log a stall with all thread stacks, once and then every REPORT_AGAIN_S."""
        last = self._reported.get(part)
        if last is not None and now - last < REPORT_AGAIN_S:
            return
        self._reported[part] = now
        logger.warning(f"Watchdog: {message}. Thread stacks:\n{thread_stacks()}")


# ── Shutdown deadline ────────────────────────────────────────────────

_deadline_lock = threading.Lock()
_deadline_timer: Optional[threading.Timer] = None


def start_shutdown_deadline(before_exit: Optional[Callable[[], None]] = None,
                            seconds: float = SHUTDOWN_DEADLINE_S,
                            exit_fn: Callable[[int], None] = os._exit) -> None:
    """End the process `seconds` from now if it is still running.

    A stop was asked for, so a thread stuck in an OS call (or an executor
    job that never returns; Python joins those at exit with no timeout)
    must not keep the process alive. Logs all thread stacks, runs
    before_exit (a WAL checkpoint), flushes the logs and exits with
    FORCED_EXIT_CODE, so the e2e check and the logs can tell it from a clean
    stop. Called again,
    it does nothing. A normal exit before the deadline ends the timer with
    the process (it is a daemon thread).
    """
    global _deadline_timer
    with _deadline_lock:
        if _deadline_timer is not None:
            return

        def _fire():
            logger.warning(f"Shutdown took over {seconds:.0f}s; forcing exit {FORCED_EXIT_CODE}. "
                         f"Thread stacks:\n{thread_stacks()}")
            if before_exit is not None:
                try:
                    before_exit()
                except Exception as e:
                    logger.error(f"Before exit: {e!r}")
            for name in ("screenmind", ""):
                for handler in logging.getLogger(name).handlers:
                    try:
                        handler.flush()
                    except Exception:
                        pass
            exit_fn(FORCED_EXIT_CODE)

        _deadline_timer = threading.Timer(seconds, _fire)
        _deadline_timer.name = "shutdown-deadline"
        _deadline_timer.daemon = True
        _deadline_timer.start()
