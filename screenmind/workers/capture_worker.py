"""
Capture Worker
Background loop that captures screenshots at regular intervals,
deduplicates them, and enqueues them for Gemma 4 analysis.
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
from screenmind.capture.window import (
    get_active_window_title, get_active_app_name, get_top_window_in, can_find_top_window,
)
from screenmind.config import settings
from screenmind.engine.a11y_extractor import A11yExtractor
from screenmind.storage.models import ScreenshotEntry

logger = logging.getLogger("screenmind.workers.capture_worker")

# Event-driven captures: at most one every MIN_EVENT_CAPTURE_GAP_S, and
# bursts of events are merged into one capture within EVENT_MERGE_WINDOW_S.
MIN_EVENT_CAPTURE_GAP_S = 3.0
EVENT_MERGE_WINDOW_S = 3.0
# Linking UI events waits on the recorder. If that hangs, capture goes on
# without actions instead of stopping with it.
LINK_UI_EVENTS_TIMEOUT_S = 5.0


@dataclass
class CaptureResult:
    """A single captured screenshot with metadata."""

    filepath: Path
    timestamp: datetime
    window_title: Optional[str] = None
    app_name: Optional[str] = None
    image: Optional[Image.Image] = None
    activity_id: Optional[int] = None
    a11y_text: Optional[str] = None  # Pre-captured at screenshot time (correct window)
    phash: Optional[object] = None  # imagehash.ImageHash for per-app cache comparison
    user_actions: Optional[str] = None  # UI events since the previous frame, as bullet lines
    browser_url: Optional[str] = None  # Page URL read from the browser itself (not OCR)


class CaptureWorker:
    """
    Background worker that captures screenshots at configurable intervals.
    Skips duplicate frames. UI events can trigger an extra capture.
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
        self._last_save_time = 0.0
        self._consecutive_skips = 0  # Track idle state
        # UI events (set by main when the recorder exists)
        self._ui_recorder = None
        self._trigger_lock = threading.Lock()
        self._pending_trigger = None  # (due_ts, first_request_ts, reason)
        self._stuck_link = None  # executor future of a UI-event link that timed out
        # Time of the last frame saved per display. A frame links only the
        # events after it, so older ones never jump to a later frame. Events
        # from before this run (still unlinked) are never linked.
        self._link_floor: dict = {}
        self._started_at = datetime.now()

    async def run(self):
        """
        Main capture loop.
        Smart polling: checks every 5s, saves only on content change.
        A periodic grab every capture_interval (default 10s), deduped like
        the others, so an unchanged screen gets no new frame.
        Idle detection: after 3+ unchanged checks, only the periodic grab.
        UI events request extra grabs, deduped too.
        """
        self._running = True
        # main.py may resume capture before run() starts, so log the real state.
        logger.info(
            f"Ready ({'paused' if self._paused else 'capturing'}). Smart capture (5s poll, "
            f"{settings.capture_interval}s max), "
            f"Saving to: {settings.screenshots_dir}"
        )
        if self._paused:
            logger.info("Click 'Start Capturing' in the dashboard to begin.")

        while self._running:
            # Read by the watchdog: a pass every <= 5 s, paused or not
            self.last_beat = time.monotonic()
            # Event-driven capture (app switch, click, typing pause)
            reason = self._take_due_trigger()
            if reason and not self._paused:
                await self._capture_tick(trigger=reason)

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
                if await self._capture_monitor(monitor, mon_app, mon_title, focused, trigger,
                                               active_title=window_title):
                    saved = True

            if not saved:
                if attempted:
                    self._consecutive_skips += 1
                return

            self._consecutive_skips = 0  # Reset idle detection
            self._last_save_time = time.time()

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
        if can_find_top_window():
            # Nothing open on this display (wallpaper only). Don't borrow the
            # focused app's name, or the model describes a page that isn't there.
            # Accessibility text would come from another display too, so treat
            # the display as unfocused.
            return None, None, False
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

    async def _capture_monitor(self, monitor, app_name, window_title, focused, trigger,
                               active_title) -> bool:
        """Capture one display. Returns True if a new frame was saved and enqueued.

        active_title is the focused window's title read before the grab.
        """
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
        window_title = filter_sensitive(window_title)

        # Read the focused window right after the grab, before the DB insert
        # and UI-event linking, so text and URL match the image.
        a11y_text, browser_url = self._read_focused(focused, active_title)

        # Insert to DB immediately so it shows in timeline right away
        activity_id = None
        if self._db:
            entry = ScreenshotEntry(
                timestamp=now,
                screenshot_path=str(filepath),
                window_title=window_title,
                detected_app_name=app_name,
                analyzed=False,
            )
            activity_id = self._db.insert_activity(entry)

        # A UI event goes to the first frame saved after it on a display,
        # if that frame shows the event's app. Otherwise it stays unlinked.
        floor_key = "focused" if monitor is None else f"{monitor['left']},{monitor['top']}"
        since = self._link_floor.get(floor_key, self._started_at)
        self._link_floor[floor_key] = now
        user_actions = await self._link_ui_events(
            activity_id, now, app_name=app_name, since=since, bounds=monitor,
        )

        capture_result = CaptureResult(
            filepath=filepath,
            timestamp=now,
            window_title=window_title,
            app_name=app_name,
            image=image,
            activity_id=activity_id,
            a11y_text=a11y_text,
            phash=dedup.last_computed_hash,
            user_actions=user_actions,
            browser_url=browser_url,
        )

        await self._queue.put(capture_result)
        self._capture_count += 1

        trigger_label = {"periodic": "[snap]", "change": "[change]"}.get(trigger, f"[event:{trigger}]")
        logger.info(
            f"{trigger_label} Captured #{self._capture_count} "
            f"({app_name or 'unknown'}: {_truncate(window_title, 50)}) "
            f"[skipped: {self._skip_count}]"
        )
        return True

    def _read_focused(self, focused, active_title):
        """A11y text and browser URL of the focused window, read just after the grab.

        Both read whatever window has focus now. If its title is no longer the
        one read before the grab (tab or app switch mid-capture), they would
        describe another window than the image, so drop them: OCR covers the
        text, and a wrong URL is worse than none.
        """
        if not focused:
            return None, None
        browser_url = _get_browser_url()
        a11y_text = None
        if self._a11y.is_available:
            a11y_text, _ = self._a11y.extract_text()
        if get_active_window_title() != active_title:
            logger.debug("Focus moved during capture; dropping a11y text and URL")
            return None, None
        return a11y_text, browser_url

    # ── UI-event-driven captures ─────────────────────────────────────

    def request_capture(self, reason: str, delay: float = 1.0):
        """Ask for a capture `delay` seconds from now. Thread-safe; called
        from the UI event recorder thread.

        Requests that arrive close together are merged: each new one pushes
        the capture back (so the screen settles), but never more than
        EVENT_MERGE_WINDOW_S after the first request. The merged request
        takes the latest reason, except that a click is never replaced, so
        the log shows which frames clicks asked for.
        """
        if self._paused:
            return
        now = time.time()
        with self._trigger_lock:
            if self._pending_trigger is None:
                self._pending_trigger = (now + delay, now, reason)
            else:
                _, first, pending = self._pending_trigger
                due = min(now + delay, first + EVENT_MERGE_WINDOW_S)
                self._pending_trigger = (due, first, "click" if pending == "click" else reason)

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

    async def _link_ui_events(self, activity_id: Optional[int], now: datetime, *,
                              app_name: Optional[str] = None, since: Optional[datetime] = None,
                              bounds: Optional[dict] = None) -> Optional[str]:
        """Attach this frame's UI events and return them as bullet lines for
        the analyzer: events of `app_name` after `since` up to `now`, and on
        this display (`bounds`) for clicks. See Database.attach_ui_events()."""
        if not activity_id or not self._db or not self._ui_recorder or not settings.ui_events_enabled:
            return None
        if self._stuck_link is not None:
            if not self._stuck_link.done():
                logger.debug("UI-event linking still stuck; frame saved without actions")
                return None
            self._stuck_link = None

        def _link():
            self._ui_recorder.flush_for_capture()
            if not self._db.attach_ui_events(activity_id, now, app_name=app_name,
                                             since=since, bounds=bounds):
                return None
            from screenmind.capture.ui_events.models import format_user_actions
            text = format_user_actions(self._db.get_ui_events(activity_id))
            if text:
                self._db.set_user_actions(activity_id, text)
            return text

        future = asyncio.get_event_loop().run_in_executor(None, _link)
        try:
            # shield: on timeout the executor future stays pending, so the
            # next frames can tell the call is still stuck.
            return await asyncio.wait_for(asyncio.shield(future), LINK_UI_EVENTS_TIMEOUT_S)
        except asyncio.TimeoutError:
            self._stuck_link = future
            future.add_done_callback(lambda f: f.cancelled() or f.exception())
            logger.warning(f"Linking UI events took over {LINK_UI_EVENTS_TIMEOUT_S:.0f}s; "
                           "capturing without actions until it returns")
            return None
        except Exception as e:
            logger.debug(f"Could not link UI events: {e}")
            return None

    def pause(self, source: str = "unknown"):
        """Pause capture (e.g., for privacy). Persists state to settings.json."""
        if self._paused:
            return  # Already paused — skip redundant persist + log
        self._paused = True
        self._dedup.reset()
        self._monitor_dedups.clear()
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
            "grab": self._screen.grab_status(),
        }


def filter_sensitive(text: Optional[str]) -> Optional[str]:
    """Redact sensitive data (per settings) before text reaches the AI or the DB."""
    if not settings.sensitive_filter_enabled or not text:
        return text
    try:
        from screenmind.privacy.data_filter import filter_sensitive_text, parse_enabled_types
        enabled_types = parse_enabled_types(settings.sensitive_filter_types)
        return filter_sensitive_text(text, enabled_types)["clean_text"]
    except Exception as e:
        logger.warning(f"Sensitive filter error: {e}")
        return text


def _get_browser_url() -> Optional[str]:
    """Current page URL from the frontmost browser, read at capture time.
    Sanitized before storage: no query strings, no sign-in/token pages."""
    try:
        from screenmind.platform_support import adapter
        from screenmind.privacy.url_filter import sanitize_url
        return sanitize_url(adapter().get_browser_url())
    except Exception:
        return None


def _truncate(text: Optional[str], max_len: int = 60) -> str:
    """Truncate a string for log display."""
    if not text:
        return ""
    return text[:max_len] + "…" if len(text) > max_len else text
