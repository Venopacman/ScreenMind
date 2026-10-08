"""
UI Event Recorder

Owns the OS backend and an enricher thread:

    OS hook thread  ->  queue.SimpleQueue  ->  enricher thread  ->  ui_events table
                                                      |
                                                      +-> CaptureWorker.request_capture()

The enricher does all slow work: accessibility lookups, text grouping,
privacy checks, DB writes. The text buffer and the pending events are
guarded by one lock, so the capture worker can call flush_for_capture()
from its own thread. Backend calls (UIA, clipboard) run without the lock:
they can hang on Windows, and a hung call must not stall capture.
"""

import logging
import queue
import threading
import time
from collections import Counter
from datetime import datetime
from typing import List, Optional

from screenmind.capture.ui_events.base import FrontWindow, UiEventBackend
from screenmind.capture.ui_events.models import (
    ElementInfo,
    EventType,
    RawEvent,
    UiEvent,
)
from screenmind.capture.ui_events.text_buffer import TextBuffer, TextChunk
from screenmind.config import settings

logger = logging.getLogger("screenmind.capture.ui_events")

PASSWORD_PLACEHOLDER = "[password field]"
MAX_CLIPBOARD_CHARS = 1000

_FRONT_POLL_S = 0.5
_CLIPBOARD_POLL_S = 1.0
_DB_FLUSH_S = 2.0
_DB_FLUSH_BATCH = 50
_FOCUS_CACHE_S = 1.0
# flush_for_capture() and stop() give up after this instead of waiting
# for a stuck enricher (e.g. a slow DB write).
_LOCK_TIMEOUT_S = 2.0
_STOP_JOIN_S = 3.0
# How long a typed label ("pwd -") waits for its value in the next chunk.
_LABEL_CARRY_S = 30.0
# Warn when a freshly installed hook gets no input for this long.
_SILENCE_WARN_S = 120.0
# How often to log what was recorded, so the user sees it in the console.
_SUMMARY_S = 600.0

# Delays before an event-driven capture, so the screen can finish updating.
TRIGGER_DELAYS = {
    "app_switch": 1.0,
    "click": 1.5,
    "typing_pause": 0.5,
    "page_change": 1.0,
    "clipboard": 0.5,
}


def create_backend() -> Optional[UiEventBackend]:
    """Return the backend for this OS, or None if it is not supported."""
    import sys
    if sys.platform == "darwin":
        try:
            from screenmind.capture.ui_events.macos import MacOSUiEventBackend
            return MacOSUiEventBackend()
        except ImportError as e:
            logger.warning(f"UI events need pyobjc (pip install pyobjc-framework-Quartz pyobjc-framework-ApplicationServices pyobjc-framework-Cocoa): {e}")
            return None
    if sys.platform == "win32":
        import importlib.util
        if importlib.util.find_spec("uiautomation") is None:
            logger.warning("UI events need uiautomation (pip install uiautomation)")
            return None
        from screenmind.capture.ui_events.windows import WindowsUiEventBackend
        return WindowsUiEventBackend()
    return None


class UiEventRecorder:
    def __init__(self, database=None, capture_worker=None, backend: Optional[UiEventBackend] = None):
        self._db = database
        self._capture_worker = capture_worker
        self._backend = backend
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        self._buffer = TextBuffer()
        self._pending: List[UiEvent] = []
        self._front: Optional[FrontWindow] = None
        self._focus: Optional[ElementInfo] = None
        self._focus_ts = 0.0
        self._clip_count: Optional[int] = None
        self._clip_check_at = 0.0
        self._last_browser_url: Optional[str] = None
        self._last_front_poll = 0.0
        self._last_clip_poll = 0.0
        self._last_db_flush = 0.0
        self._events_recorded = 0
        self._last_error: Optional[str] = None
        # (field identity, end time, label) of a chunk that ended in "pwd -".
        self._open_label: Optional[tuple] = None
        # Health and counters, logged so a silent failure shows up in the console.
        self._off_logged = False
        self._started_at = 0.0
        self._input_events = 0
        self._first_input_seen = False
        self._silence_warned = False
        self._hook_dead_warned = False
        self._seen_callback_errors = 0
        self._seen_reenabled = 0
        self._skipped: Counter = Counter()
        self._last_summary = 0.0
        self._period_input = 0
        self._period_stored: Counter = Counter()
        self._period_skipped: Counter = Counter()
        # Enricher loop passes; the watchdog reads it to see a stuck enricher.
        self.ticks = 0

    # ── Lifecycle ────────────────────────────────────────────────────

    @property
    def supported(self) -> bool:
        return self._backend is not None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def sync_with_settings(self):
        """Start or stop to match settings.ui_events_enabled."""
        if settings.ui_events_enabled:
            if not self.running:
                self.start()
            return
        if self.running:
            self.stop()
        if not self._off_logged:
            self._off_logged = True
            logger.info("UI events are off (ui_events_enabled=false). Clicks and app switches "
                        "are not recorded. Turn them on in Settings > Privacy & Security > UI Events.")

    def start(self) -> bool:
        if not self.supported:
            logger.info("UI events are not supported on this platform yet")
            return False
        if self.running:
            return True
        perms = self._backend.check_permissions()
        if not perms.accessibility:
            logger.warning("UI events: no Accessibility permission. Clicks are stored without "
                           "the button or field name, and typed text is not recorded. Grant "
                           "Accessibility to the app that starts ScreenMind (e.g. Terminal) "
                           "and restart ScreenMind.")
        # Missing Input Monitoring is reported by the backend: it knows
        # whether it fell back to clicks only.
        if not self._backend.start(self._queue):
            self._last_error = "Could not install the input hook"
            logger.warning("UI events: could not install the input hook. Nothing will be recorded.")
            return False
        self._clip_count = self._backend.clipboard_change_count()
        self._off_logged = False
        self._started_at = self._last_summary = time.time()
        self._first_input_seen = self._silence_warned = self._hook_dead_warned = False
        # A new event per thread: an old enricher still stuck in a backend
        # call must not come back to life when we restart.
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._stop,),
                                        name="ui-events-enricher", daemon=True)
        self._thread.start()
        types = settings.ui_events_types_list
        logger.info(f"UI event recording started (types: {', '.join(types) or 'none'})")
        if not types:
            logger.warning("UI events: ui_events_types is empty, so nothing will be recorded.")
        return True

    def stop(self):
        if not self.running:
            return
        # Set the stop flag before the hook goes down, so the enricher does
        # not take our own stop for a dead hook.
        self._stop.set()
        self._backend.stop()
        self._thread.join(timeout=_STOP_JOIN_S)
        if self._thread.is_alive():
            logger.warning("UI events: enricher thread is stuck; leaving it behind")
        self._thread = None
        if self._flush_locked():
            logger.info("UI event recording stopped")
        else:
            logger.warning("UI events: stopped without writing the last events")

    def restart(self) -> bool:
        """Stop and start again, for the watchdog when the enricher is stuck.
        A stuck enricher may hold the lock for good, so the new one gets a
        new lock; the old thread's events still pending are dropped."""
        old = self._thread
        self.stop()
        if old is not None and old.is_alive():
            self._lock = threading.RLock()
            self._pending = []
            self._buffer = TextBuffer()
        return self.start()

    def status(self) -> dict:
        perms = self._backend.check_permissions().as_dict() if self.supported else None
        stats = self._backend.tap_stats() if self.supported else {}
        return {
            "supported": self.supported,
            "backend": self._backend.name if self.supported else None,
            "enabled": settings.ui_events_enabled,
            "running": self.running,
            "hook_running": self.supported and self.running and self._backend.is_running(),
            "keys_tapped": stats.get("keys_tapped"),
            "permissions": perms,
            "input_events": self._input_events,
            "events_recorded": self._events_recorded,
            "skipped": dict(self._skipped),
            "last_error": self._last_error,
        }

    def request_permissions(self) -> Optional[dict]:
        if not self.supported:
            return None
        return self._backend.request_permissions().as_dict()

    def flush_for_capture(self):
        """Write everything up to now to the DB. Called by the capture worker
        right before it links events to a new frame."""
        if not self._flush_locked():
            logger.debug("UI events: recorder busy, frame linked without the latest events")

    def _flush_locked(self) -> bool:
        """Flush the text buffer and pending events. False if the lock was
        not free within _LOCK_TIMEOUT_S; the events stay queued then."""
        if not self._lock.acquire(timeout=_LOCK_TIMEOUT_S):
            return False
        try:
            self._emit_chunk(self._buffer.flush(), trigger=False)
            self._write_pending()
        finally:
            self._lock.release()
        return True

    # ── Enricher loop ────────────────────────────────────────────────

    def _run(self, stop: threading.Event):
        while not stop.is_set():
            try:
                raw = self._queue.get(timeout=0.25)
            except queue.Empty:
                raw = None
            try:
                self._tick(raw, time.time())
            except Exception as e:
                self._last_error = str(e)
                logger.error(f"UI event processing failed: {e}")

    def _tick(self, raw: Optional[RawEvent], now: float):
        self.ticks += 1
        if now - self._last_front_poll >= _FRONT_POLL_S:
            self._poll_front(now)
        if raw is not None:
            if raw.kind == "mouse_down":
                self._on_click(raw)
            elif raw.kind == "key":
                self._on_key(raw, now)
        if now - self._last_clip_poll >= _CLIPBOARD_POLL_S or (
            self._clip_check_at and now >= self._clip_check_at
        ):
            self._poll_clipboard(now)
        with self._lock:
            self._emit_chunk(self._buffer.poll(now), trigger=True)
            if self._pending and (
                now - self._last_db_flush >= _DB_FLUSH_S or len(self._pending) >= _DB_FLUSH_BATCH
            ):
                self._write_pending()
        self._check_health(raw, now)

    # ── Health ───────────────────────────────────────────────────────

    def _check_health(self, raw: Optional[RawEvent], now: float):
        """Log what would otherwise fail silently: no input arriving, events
        lost in the hook, a dead hook. Runs on the enricher thread."""
        if raw is not None:
            self._input_events += 1
            self._period_input += 1
            if not self._first_input_seen:
                self._first_input_seen = True
                logger.info(f"UI events: first input event received ({raw.kind})")
        elif (self._started_at and not self._first_input_seen and not self._silence_warned
              and now - self._started_at >= _SILENCE_WARN_S):
            self._silence_warned = True
            logger.warning(f"UI events: no clicks or keys arrived in {int(_SILENCE_WARN_S)}s. "
                           "If you clicked in that time, the OS is not sending input to "
                           "ScreenMind. On macOS, check Input Monitoring for the app that "
                           "starts ScreenMind (e.g. Terminal) and restart ScreenMind.")

        stats = self._backend.tap_stats()
        errors = stats.get("callback_errors", 0)
        if errors > self._seen_callback_errors:
            logger.warning(f"UI events: {errors - self._seen_callback_errors} input events lost "
                           f"in the hook (last error: {stats.get('last_callback_error')})")
            self._seen_callback_errors = errors
        reenabled = stats.get("reenabled", 0)
        if reenabled > self._seen_reenabled:
            logger.info(f"UI events: the OS turned the input hook off {reenabled - self._seen_reenabled} "
                        "time(s); turned it back on")
            self._seen_reenabled = reenabled

        # Read the stop flag after is_running(): stop() sets it before it
        # stops the hook, so a hook stopped by stop() is never seen as dead.
        if (self._started_at and not self._hook_dead_warned
                and not self._backend.is_running() and not self._stop.is_set()):
            self._hook_dead_warned = True
            self._last_error = "The input hook stopped"
            logger.warning("UI events: the input hook stopped. Clicks and keys are no longer "
                           "recorded. Restart ScreenMind.")

        if self._started_at and now - self._last_summary >= _SUMMARY_S:
            self._log_summary(now)

    def _log_summary(self, now: float):
        self._last_summary = now
        if not (self._period_input or self._period_stored or self._period_skipped):
            return
        stored = ", ".join(f"{k} {v}" for k, v in sorted(self._period_stored.items())) or "nothing"
        msg = (f"UI events, last {int(_SUMMARY_S // 60)} min: {self._period_input} clicks/keys in, "
               f"stored {stored}")
        if self._period_skipped:
            msg += "; skipped: " + ", ".join(f"{v} ({k})" for k, v in sorted(self._period_skipped.items()))
        logger.info(msg)
        self._period_input = 0
        self._period_stored.clear()
        self._period_skipped.clear()

    def _skip(self, reason: str):
        self._skipped[reason] += 1
        self._period_skipped[reason] += 1

    # ── Gates ────────────────────────────────────────────────────────

    def _type_enabled(self, t: EventType) -> bool:
        return t.value in settings.ui_events_types_list

    def _capture_active(self) -> bool:
        cw = self._capture_worker
        return cw is None or not cw.is_paused

    def _app_blocked(self, app_name: Optional[str]) -> bool:
        return bool(app_name) and app_name.lower() in settings.blocked_apps_list

    def _should_record(self, app_name: Optional[str]) -> bool:
        if not self._capture_active():
            self._skip("capture paused")
            return False
        if self._app_blocked(app_name):
            self._skip("blocked app")
            return False
        return True

    # ── Handlers ─────────────────────────────────────────────────────

    def _poll_front(self, now: float):
        self._last_front_poll = now
        front = self._backend.front_window()
        if front is None:
            return
        prev = self._front
        self._front = front
        if prev is None:
            return
        if front.pid != prev.pid or front.app_name != prev.app_name:
            self._end_chunk()
            self._focus = None
            self._last_browser_url = None
            if self._type_enabled(EventType.APP_SWITCH) and self._should_record(front.app_name):
                url = self._page_url()
                self._last_browser_url = url
                self._add(EventType.APP_SWITCH, now, front.app_name, front.title, url=url)
                self._trigger("app_switch")
        elif front.title != prev.title and front.title:
            if not self._should_record(front.app_name):
                return
            url = self._page_url()
            page_changed = bool(url) and url != self._last_browser_url
            if url:
                self._last_browser_url = url
            # Browsers: record tab switches and navigation even when
            # window_focus is off, but only when the URL changed. Page
            # titles alone change all the time ("(3) Slack").
            if self._type_enabled(EventType.WINDOW_FOCUS) or page_changed:
                self._add(EventType.WINDOW_FOCUS, now, front.app_name, front.title, url=url)
            if page_changed:
                self._trigger("page_change")

    def _on_click(self, raw: RawEvent):
        # A click moves the text cursor, so the current chunk ends here.
        self._end_chunk()
        self._focus = None
        if not self._type_enabled(EventType.CLICK):
            return
        element = self._backend.element_at(raw.x, raw.y)
        app_name = self._app_for(element)
        if not self._should_record(app_name):
            return
        in_front = bool(self._front) and self._front.app_name == app_name
        window = self._front.title if in_front else None
        url = self._page_url() if in_front else None
        if element and element.is_password:
            self._add(EventType.CLICK, raw.ts, app_name, window,
                      element_role=element.role, element_name=PASSWORD_PLACEHOLDER,
                      x=raw.x, y=raw.y, url=url)
        else:
            self._add(EventType.CLICK, raw.ts, app_name, window,
                      element_role=element.role if element else None,
                      element_name=element.name if element else None,
                      element_value=element.value if element else None,
                      x=raw.x, y=raw.y, url=url)
        # Every click asks for a frame, a click into a text field too
        self._trigger("click")

    def _on_key(self, raw: RawEvent, now: float):
        if raw.shortcut and raw.shortcut_char in ("c", "x"):
            # Check the clipboard soon instead of waiting for the next poll.
            self._clip_check_at = now + 0.3
        if not self._type_enabled(EventType.TEXT):
            return
        target = self._focused(now)
        if target is None or not target.is_text_input:
            # Only record typing into real text inputs. Without a known
            # focused element we cannot tell a password field from a normal
            # one, and keys sent to windows, lists or games are not text.
            self._end_chunk()
            return
        app_name = self._app_for(target)
        if not self._should_record(app_name):
            self._end_chunk()
            return
        window = self._front.title if self._front and self._front.app_name == app_name else None
        with self._lock:
            for chunk in self._buffer.add(raw, target, app_name, window):
                self._emit_chunk(chunk, trigger=raw.key == "enter")

    def _poll_clipboard(self, now: float):
        self._last_clip_poll = now
        self._clip_check_at = 0.0
        count = self._backend.clipboard_change_count()
        if count is None or count == self._clip_count:
            return
        self._clip_count = count
        if not self._type_enabled(EventType.CLIPBOARD):
            return
        app_name = self._front.app_name if self._front else None
        if not self._should_record(app_name):
            return
        text = self._backend.read_clipboard()
        if not text or not text.strip():
            return
        text = text.strip()[:MAX_CLIPBOARD_CHARS]
        self._add(EventType.CLIPBOARD, now, app_name,
                  self._front.title if self._front else None, text=text)
        self._trigger("clipboard")

    # ── Helpers ──────────────────────────────────────────────────────

    def _focused(self, now: float) -> Optional[ElementInfo]:
        # Focus only changes through clicks, Tab and shortcuts, and all of
        # those reset the cache, so a short cache is safe while typing.
        if self._focus is None or now - self._focus_ts >= _FOCUS_CACHE_S:
            self._focus = self._backend.focused_element()
            self._focus_ts = now
        return self._focus

    def _page_url(self) -> Optional[str]:
        """Browser page URL of the frontmost window (None for other apps).
        Sanitized like active_url: no query strings, no sign-in/token pages."""
        try:
            from screenmind.privacy.url_filter import sanitize_url
            return sanitize_url(self._backend.browser_url())
        except Exception:
            return None

    def _app_for(self, element: Optional[ElementInfo]) -> Optional[str]:
        if element and element.pid:
            if self._front and element.pid == self._front.pid:
                return self._front.app_name
            name = self._backend.app_name_for_pid(element.pid)
            if name:
                return name
        return self._front.app_name if self._front else None

    def _clean(self, text: Optional[str], after_label: Optional[str] = None) -> Optional[str]:
        if not text:
            return text
        if not settings.sensitive_filter_enabled:
            return text
        from screenmind.privacy.data_filter import filter_after_label, parse_enabled_types
        return filter_after_label(after_label, text, parse_enabled_types(settings.sensitive_filter_types))

    def _end_chunk(self):
        """Store the text typed so far (focus moved, app changed...)."""
        with self._lock:
            self._emit_chunk(self._buffer.flush(), trigger=False)

    def _emit_chunk(self, chunk: Optional[TextChunk], trigger: bool):
        """Queue one text chunk as an event. Caller holds self._lock."""
        if chunk is None:
            return
        ts = chunk.end_ts
        label, self._open_label = self._open_label, None
        if chunk.is_password:
            self._add(EventType.TEXT, ts, chunk.app_name, chunk.window_title,
                      element_role=chunk.target.role, text=PASSWORD_PLACEHOLDER)
            return
        self._add(EventType.TEXT, ts, chunk.app_name, chunk.window_title,
                  element_role=chunk.target.role,
                  element_name=chunk.target.name,
                  text=self._typed_text(chunk, label))
        if trigger:
            self._trigger("typing_pause")

    def _typed_text(self, chunk: TextChunk, label: Optional[tuple]) -> str:
        """Chunk text, with a value redacted if it belongs to a label typed
        just before. Enter or a pause can split "pwd -" and the value into
        two chunks, so a label left open by the previous chunk in the same
        field is filtered together with this one. _add() filters the rest."""
        from screenmind.privacy.data_filter import dangling_secret_label
        text = chunk.text
        if (label and label[0] == chunk.target.identity()
                and chunk.start_ts - label[1] <= _LABEL_CARRY_S):
            text = self._clean(text, after_label=label[2])
        tail = dangling_secret_label(text)
        if tail:
            self._open_label = (chunk.target.identity(), chunk.end_ts, tail)
        return text

    def _add(self, type_: EventType, ts: float, app_name, window_title, **fields):
        # Every free-text field goes through the sensitive-data filter here,
        # before it is queued for the DB.
        window_title = self._clean(window_title)
        for key in ("element_name", "element_value", "text", "url"):
            if fields.get(key):
                fields[key] = self._clean(fields[key])
        if fields.get("x") is not None:
            fields["x"] = int(fields["x"])
        if fields.get("y") is not None:
            fields["y"] = int(fields["y"])
        event = UiEvent(
            timestamp=datetime.fromtimestamp(ts),
            type=type_,
            app_name=app_name,
            window_title=window_title,
            **fields,
        )
        with self._lock:
            self._pending.append(event)

    def _write_pending(self):
        """Store the queued events. Caller holds self._lock, so a frame
        never links while a write is half done."""
        self._last_db_flush = time.time()
        if not self._pending:
            return
        events, self._pending = self._pending, []
        if self._db is None:
            return
        try:
            self._db.insert_ui_events(events)
            self._events_recorded += len(events)
            self._period_stored.update(e.type.value for e in events)
        except Exception as e:
            self._last_error = str(e)
            logger.error(f"Could not store {len(events)} UI events: {e}")

    def _trigger(self, reason: str):
        cw = self._capture_worker
        if cw is None or not settings.event_triggered_capture:
            return
        try:
            cw.request_capture(reason, TRIGGER_DELAYS.get(reason, 1.0))
        except Exception as e:
            logger.debug(f"Capture trigger failed: {e}")
