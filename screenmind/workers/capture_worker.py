"""
Capture Worker
Background loop that captures screenshots at regular intervals,
deduplicates them, and enqueues them for Gemma 4 analysis.
Also handles hotkey-triggered instant bookmarked captures.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import Image

from screenmind.capture.screen import ScreenCapture
from screenmind.capture.dedup import ScreenDeduplicator
from screenmind.capture.window import get_active_window_title, get_active_app_name, get_top_window_in
from screenmind.config import settings
from screenmind.engine.a11y_extractor import A11yExtractor
from screenmind.storage.models import ScreenshotEntry

logger = logging.getLogger("screenmind.workers.capture_worker")


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
    browser_url: Optional[str] = None  # Page URL read from the browser itself (not OCR)


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
        self._dedup = ScreenDeduplicator(threshold=8)  # single-display path
        self._monitor_dedups: dict = {}  # monitor key -> ScreenDeduplicator (all-displays path)
        self._a11y = A11yExtractor()  # Runs at capture time for correct window
        self._paused = True  # Start paused — user must explicitly start capture
        self._running = False
        self._capture_count = 0
        self._skip_count = 0
        self._pending_bookmark = False
        self._last_save_time = 0.0
        self._consecutive_skips = 0  # Track idle state

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
                if not self._running:
                    break
                await asyncio.sleep(0.5)

    async def _capture_tick(self, trigger="periodic"):
        """Capture a screenshot of each display and enqueue the ones that changed."""
        try:
            app_name = get_active_app_name()

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

            monitors = self._screen.monitors_to_capture()
            active = self._screen.active_monitor() if len(monitors) > 1 else None
            attempted = saved = False
            for monitor in monitors:
                mon_app, mon_title, focused = self._label_monitor(
                    monitor, active, app_name, window_title
                )

                # Check privacy zones
                if self._is_blocked(mon_app) or (mon_app is None and self._is_blocked(app_name)):
                    continue

                attempted = True
                if await self._capture_monitor(monitor, mon_app, mon_title, focused, trigger):
                    saved = True

            if not saved:
                if attempted:
                    self._consecutive_skips += 1
                return

            self._consecutive_skips = 0  # Reset idle detection
            self._last_save_time = time.time()

            # Smart notifications check
            try:
                from screenmind.integrations.smart_notify import check as notify_check
                notify_check(app_name or "")
            except Exception:
                pass

        except Exception as e:
            logger.error(f"Error: {e}")

    def _is_blocked(self, app_name: Optional[str]) -> bool:
        return bool(app_name) and app_name.lower() in [
            a.lower() for a in settings.blocked_apps_list
        ]

    def _label_monitor(self, monitor, active, app_name, window_title):
        """Return (app, title, focused) for the app shown on this display.

        monitor=None is the single-display path: the focused app labels it.
        With several displays, each one is labeled with its own top window, so
        a call on one screen and an editor on the other get separate entries.
        """
        if monitor is None:
            return app_name, window_title, True

        focused = monitor == active
        top = get_top_window_in(monitor)
        if top:
            return top[0], top[1], focused
        # Adapter can't tell (Windows/Linux): only the focused display is known
        if focused:
            return app_name, window_title, True
        return None, None, False

    def _dedup_for(self, monitor) -> ScreenDeduplicator:
        if monitor is None:
            return self._dedup
        key = f"{monitor['left']},{monitor['top']}"
        if key not in self._monitor_dedups:
            self._monitor_dedups[key] = ScreenDeduplicator(threshold=8)
        return self._monitor_dedups[key]

    async def _capture_monitor(self, monitor, app_name, window_title, focused, trigger) -> bool:
        """Capture one display. Returns True if a new frame was saved and enqueued."""
        result = self._screen.capture(monitor)
        if result is None:
            return False

        filepath, image = result

        # Check for duplicate (content-change detection)
        dedup = self._dedup_for(monitor)
        # Per-display dedups never switch monitors; the single one may (active-monitor mode)
        monitor_key = self._screen.last_monitor_key if monitor is None else None
        if dedup.is_duplicate(image, monitor_key=monitor_key):
            self._skip_count += 1
            try:
                filepath.unlink()
            except OSError:
                pass
            return False

        # Content has changed! Save it.
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

        # A11y extraction — MUST happen now while window is still focused
        # This fixes the wrong-window bug (analysis worker runs later).
        # It reads the focused window, so skip it for the other displays.
        a11y_text = None
        if focused and self._a11y.is_available:
            a11y_text, _ = self._a11y.extract_text()
        browser_url = _get_browser_url() if focused else None

        capture_result = CaptureResult(
            filepath=filepath,
            timestamp=now,
            window_title=window_title,
            app_name=app_name,
            bookmarked=False,
            image=image,
            activity_id=activity_id,
            a11y_text=a11y_text,
            phash=dedup.last_computed_hash,
            browser_url=browser_url,
        )

        await self._queue.put(capture_result)
        self._capture_count += 1

        trigger_label = "[snap]" if trigger == "periodic" else "[change]"
        logger.info(
            f"{trigger_label} Captured #{self._capture_count} "
            f"({app_name or 'unknown'}: {_truncate(window_title, 50)}) "
            f"[skipped: {self._skip_count}]"
        )
        return True

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
        """Perform an immediate bookmarked capture of each display."""
        window_title = get_active_window_title()
        app_name = get_active_app_name()

        monitors = self._screen.monitors_to_capture()
        active = self._screen.active_monitor() if len(monitors) > 1 else None
        for monitor in monitors:
            mon_app, mon_title, focused = self._label_monitor(
                monitor, active, app_name, window_title
            )
            await self._bookmark_monitor(monitor, mon_app, mon_title, focused)

    async def _bookmark_monitor(self, monitor, app_name, window_title, focused):
        result = self._screen.capture(monitor)
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

        # A11y extraction at capture time (correct window)
        a11y_text = None
        if focused and self._a11y.is_available:
            a11y_text, _ = self._a11y.extract_text()
        browser_url = _get_browser_url() if focused else None

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
            browser_url=browser_url,
        )

        await self._queue.put(capture_result)
        self._capture_count += 1
        logger.info(f"[*] Bookmarked capture #{self._capture_count}")

    def pause(self, source: str = "unknown"):
        """Pause capture (e.g., for privacy). Persists state to settings.json."""
        if self._paused:
            return  # Already paused — skip redundant persist + log
        self._paused = True
        self._dedup.reset()
        self._monitor_dedups.clear()
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


def _get_browser_url() -> Optional[str]:
    """Current page URL from the frontmost browser, read at capture time."""
    try:
        from screenmind.platform_support import adapter
        return adapter().get_browser_url()
    except Exception:
        return None


def _truncate(text: Optional[str], max_len: int = 60) -> str:
    """Truncate a string for log display."""
    if not text:
        return ""
    return text[:max_len] + "…" if len(text) > max_len else text
