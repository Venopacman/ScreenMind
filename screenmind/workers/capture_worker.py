"""
Capture Worker
Background loop that captures screenshots at regular intervals,
deduplicates them, and enqueues them for Gemma 4 analysis.
Also handles hotkey-triggered instant bookmarked captures.
"""

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import Image

from screenmind.capture.screen import ScreenCapture
from screenmind.capture.dedup import ScreenDeduplicator
from screenmind.capture.window import get_active_window_title, get_active_app_name
from screenmind.config import settings
from screenmind.engine.a11y_extractor import A11yExtractor
from screenmind.storage.models import ScreenshotEntry

logger = logging.getLogger("screenmind.workers.capture_worker")

# Event-driven captures: at most one every MIN_EVENT_CAPTURE_GAP_S, and
# bursts of events are merged into one capture within EVENT_MERGE_WINDOW_S.
MIN_EVENT_CAPTURE_GAP_S = 3.0
EVENT_MERGE_WINDOW_S = 3.0


@dataclass
class CaptureResult:
    """A single captured screenshot with metadata."""

    filepath: Path
    timestamp: datetime
    window_title: Optional[str] = None
    app_name: Optional[str] = None
    bookmarked: bool = False
    image: Optional[Image.Image] = None
    activity_id: Optional[int] = None
    a11y_text: Optional[str] = None  # Pre-captured at screenshot time (correct window)
    phash: Optional[object] = None  # imagehash.ImageHash for per-app cache comparison
    user_actions: Optional[str] = None  # UI events since the previous frame, as bullet lines


class CaptureWorker:
    """
    Background worker that captures screenshots at configurable intervals.
    Skips duplicate frames. Supports hotkey-triggered instant captures.
    """

    def __init__(self, queue: asyncio.Queue, database=None):
        """
        Args:
            queue: Async queue to push CaptureResult items for downstream processing.
            database: Database instance for immediate activity insertion.
        """
        self._queue = queue
        self._db = database
        self._screen = ScreenCapture()
        self._dedup = ScreenDeduplicator(threshold=8)
        self._a11y = A11yExtractor()  # Runs at capture time for correct window
        self._paused = True  # Start paused — user must explicitly start capture
        self._running = False
        self._capture_count = 0
        self._skip_count = 0
        self._pending_bookmark = False
        self._last_save_time = 0.0
        self._consecutive_skips = 0  # Track idle state
        # UI events (set by main when the recorder exists)
        self._ui_recorder = None
        self._trigger_lock = threading.Lock()
        self._pending_trigger = None  # (due_ts, first_request_ts, reason)

    async def run(self):
        """
        Main capture loop.
        Smart polling: checks every 5s, saves only on content change.
        Forces a capture every 30s (capture_interval) even if no change.
        Idle detection: if screen unchanged for 3+ checks, extends poll to 30s.
        """
        self._running = True
        logger.info(
            f"Ready (paused). Smart capture (5s poll, "
            f"{settings.capture_interval}s max), "
            f"Saving to: {settings.screenshots_dir}"
        )
        logger.info("Press Ctrl+Shift+P or click 'Start Capturing' in the dashboard to begin.")

        while self._running:
            # Handle pending bookmark captures
            if hasattr(self, '_pending_bookmark') and self._pending_bookmark:
                self._pending_bookmark = False
                await self._do_bookmark_capture()

            # Event-driven capture (app switch, click, typing pause)
            reason = self._take_due_trigger()
            if reason and not self._paused:
                await self._capture_tick(trigger=reason)

            # Meeting detection runs ALWAYS (even when paused)
            try:
                if hasattr(self, '_audio_worker') and self._audio_worker:
                    from screenmind.capture.window import get_active_app_name as _get_app
                    _app = _get_app()
                    if _app:
                        await asyncio.get_event_loop().run_in_executor(
                            None, self._audio_worker.check_meeting, _app
                        )
            except Exception:
                pass

            if not self._paused:
                now = time.time()
                since_last = now - self._last_save_time

                # Idle detection: if 3+ consecutive skips, user is idle
                # Extend interval to capture_interval (30s) instead of 5s polling
                if self._consecutive_skips >= 3:
                    # Idle mode — only check at max interval
                    if since_last >= settings.capture_interval:
                        await self._capture_tick(trigger="periodic")
                else:
                    # Active mode — normal polling
                    if since_last >= settings.capture_interval:
                        await self._capture_tick(trigger="periodic")
                    elif since_last >= 10:
                        # Content-change check: 10s cooldown prevents burst
                        # captures during scrolling while still catching
                        # meaningful screen changes promptly
                        await self._capture_tick(trigger="change")
                # else: too soon, skip

            # Poll every 5 seconds (fast enough to catch content changes)
            for _ in range(10):
                if not self._running or self._trigger_due():
                    break
                await asyncio.sleep(0.5)

    async def _capture_tick(self, trigger="periodic"):
        """Capture a screenshot and enqueue if content changed."""
        try:
            # Check privacy zones
            app_name = get_active_app_name()
            if app_name and app_name.lower() in [
                a.lower() for a in settings.blocked_apps_list
            ]:
                return

            # Auto-pause for heavy apps (games, video editors)
            if settings.auto_pause_heavy_apps and app_name:
                app_lower = app_name.lower()
                for heavy in settings.heavy_apps_list:
                    if heavy in app_lower:
                        if not getattr(self, '_heavy_app_logged', None) == app_name:
                            self._heavy_app_logged = app_name
                            logger.debug(f"Auto-paused (heavy app: {app_name})")
                        return
                # Clear the log flag when returning to normal apps
                if hasattr(self, '_heavy_app_logged'):
                    self._heavy_app_logged = None

            window_title = get_active_window_title()

            # Capture screenshot
            result = self._screen.capture()
            if result is None:
                return

            filepath, image = result

            # Check for duplicate (content-change detection)
            monitor_key = self._screen.last_monitor_key
            if self._dedup.is_duplicate(image, monitor_key=monitor_key):
                self._skip_count += 1
                self._consecutive_skips += 1
                try:
                    filepath.unlink()
                except OSError:
                    pass
                return

            # Content has changed! Save it.
            self._consecutive_skips = 0  # Reset idle detection
            now = datetime.now()

            # Insert to DB immediately so it shows in timeline right away
            activity_id = None
            if self._db:
                entry = ScreenshotEntry(
                    timestamp=now,
                    screenshot_path=str(filepath),
                    window_title=window_title,
                    detected_app_name=app_name,
                    bookmarked=False,
                    analyzed=False,
                )
                activity_id = self._db.insert_activity(entry)

            user_actions = await self._link_ui_events(activity_id, now)

            # A11y extraction — MUST happen now while window is still focused
            # This fixes the wrong-window bug (analysis worker runs later)
            a11y_text = None
            if self._a11y.is_available:
                a11y_text, _ = self._a11y.extract_text()

            capture_result = CaptureResult(
                filepath=filepath,
                timestamp=now,
                window_title=window_title,
                app_name=app_name,
                bookmarked=False,
                image=image,
                activity_id=activity_id,
                a11y_text=a11y_text,
                phash=self._dedup.last_computed_hash,
                user_actions=user_actions,
            )

            await self._queue.put(capture_result)
            self._capture_count += 1
            self._last_save_time = time.time()

            trigger_label = {"periodic": "[snap]", "change": "[change]"}.get(trigger, f"[event:{trigger}]")
            logger.info(
                f"{trigger_label} Captured #{self._capture_count} "
                f"({app_name or 'unknown'}: {_truncate(window_title, 50)}) "
                f"[skipped: {self._skip_count}]"
            )

            # Smart notifications check
            try:
                from screenmind.integrations.smart_notify import check as notify_check
                notify_check(app_name or "")
            except Exception:
                pass

        except Exception as e:
            logger.error(f"Error: {e}")

    def trigger_bookmark(self):
        """
        Called by the hotkey listener. Triggers an immediate capture
        with the bookmarked flag set. Thread-safe via asyncio.
        """
        try:
            self._pending_bookmark = True
            logger.info("Bookmark capture queued.")
        except Exception as e:
            logger.error(f"Bookmark error: {e}")

    async def _do_bookmark_capture(self):
        """Perform an immediate bookmarked capture."""
        window_title = get_active_window_title()
        app_name = get_active_app_name()

        result = self._screen.capture()
        if result is None:
            return

        filepath, image = result

        # Always process bookmarked captures — never dedup
        now = datetime.now()

        # Insert to DB immediately
        activity_id = None
        if self._db:
            entry = ScreenshotEntry(
                timestamp=now,
                screenshot_path=str(filepath),
                window_title=window_title,
                detected_app_name=app_name,
                bookmarked=True,
                analyzed=False,
            )
            activity_id = self._db.insert_activity(entry)

        user_actions = await self._link_ui_events(activity_id, now)

        # A11y extraction at capture time (correct window)
        a11y_text = None
        if self._a11y.is_available:
            a11y_text, _ = self._a11y.extract_text()

        # Compute pHash for bookmark captures (dedup doesn't run for bookmarks)
        import imagehash
        bookmark_phash = imagehash.phash(image)

        capture_result = CaptureResult(
            filepath=filepath,
            timestamp=now,
            window_title=window_title,
            app_name=app_name,
            bookmarked=True,
            image=image,
            activity_id=activity_id,
            a11y_text=a11y_text,
            phash=bookmark_phash,
            user_actions=user_actions,
        )

        await self._queue.put(capture_result)
        self._capture_count += 1
        logger.info(f"[*] Bookmarked capture #{self._capture_count}")

    # ── UI-event-driven captures ─────────────────────────────────────

    def request_capture(self, reason: str, delay: float = 1.0):
        """Ask for a capture `delay` seconds from now. Thread-safe; called
        from the UI event recorder thread.

        Requests that arrive close together are merged: each new one pushes
        the capture back (so the screen settles), but never more than
        EVENT_MERGE_WINDOW_S after the first request.
        """
        if self._paused:
            return
        now = time.time()
        with self._trigger_lock:
            if self._pending_trigger is None:
                self._pending_trigger = (now + delay, now, reason)
            else:
                _, first, _ = self._pending_trigger
                due = min(now + delay, first + EVENT_MERGE_WINDOW_S)
                self._pending_trigger = (due, first, reason)

    def _trigger_due(self) -> bool:
        with self._trigger_lock:
            return self._pending_trigger is not None and time.time() >= self._pending_trigger[0]

    def _take_due_trigger(self) -> Optional[str]:
        """Pop the pending trigger if it is due and the rate limit allows it."""
        now = time.time()
        with self._trigger_lock:
            if self._pending_trigger is None or now < self._pending_trigger[0]:
                return None
            _, first, reason = self._pending_trigger
            earliest = self._last_save_time + MIN_EVENT_CAPTURE_GAP_S
            if now < earliest:
                self._pending_trigger = (earliest, first, reason)
                return None
            self._pending_trigger = None
            return reason

    async def _link_ui_events(self, activity_id: Optional[int], now: datetime) -> Optional[str]:
        """Attach UI events recorded up to `now` to this frame and return
        them as bullet lines for the analyzer."""
        if not activity_id or not self._db or not self._ui_recorder or not settings.ui_events_enabled:
            return None

        def _link():
            self._ui_recorder.flush_for_capture()
            if not self._db.attach_ui_events(activity_id, now):
                return None
            from screenmind.capture.ui_events.models import format_user_actions
            text = format_user_actions(self._db.get_ui_events(activity_id))
            if text:
                self._db.set_user_actions(activity_id, text)
            return text

        try:
            return await asyncio.get_event_loop().run_in_executor(None, _link)
        except Exception as e:
            logger.debug(f"Could not link UI events: {e}")
            return None

    def pause(self, source: str = "unknown"):
        """Pause capture (e.g., for privacy). Persists state to settings.json."""
        if self._paused:
            return  # Already paused — skip redundant persist + log
        self._paused = True
        self._dedup.reset()
        with self._trigger_lock:
            self._pending_trigger = None
        try:
            settings.save_runtime_overrides({"capture_paused": True})
        except Exception:
            pass  # Never fail capture operations due to settings persistence
        logger.info(f"Paused. (source: {source})")

    def resume(self, source: str = "unknown"):
        """Resume capture. Persists state to settings.json."""
        if not self._paused:
            return  # Already running — skip redundant persist + log
        self._paused = False
        try:
            settings.save_runtime_overrides({"capture_paused": False})
        except Exception:
            pass  # Never fail capture operations due to settings persistence
        logger.info(f"Resumed. (source: {source})")

    def stop(self):
        """Stop the capture worker."""
        self._running = False
        logger.info(
            f"Stopped. "
            f"Total captures: {self._capture_count}, "
            f"Skipped: {self._skip_count}"
        )

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def stats(self) -> dict:
        return {
            "running": self._running,
            "paused": self._paused,
            "captures": self._capture_count,
            "skipped": self._skip_count,
        }


def _truncate(text: Optional[str], max_len: int = 60) -> str:
    """Truncate a string for log display."""
    if not text:
        return ""
    return text[:max_len] + "…" if len(text) > max_len else text
