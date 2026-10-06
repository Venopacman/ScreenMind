"""
UI Event Recorder

Owns the OS backend and an enricher thread:

    OS hook thread  ->  queue.SimpleQueue  ->  enricher thread  ->  ui_events table
                                                      |
                                                      +-> CaptureWorker.request_capture()

The enricher does all slow work: accessibility lookups, text grouping,
privacy checks, DB writes. All its state is guarded by one lock, so the
capture worker can call flush_for_capture() from the asyncio thread.
"""

import logging
import queue
import threading
import time
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

# Delays before an event-driven capture, so the screen can finish updating.
TRIGGER_DELAYS = {
    "app_switch": 1.0,
    "click": 1.5,
    "typing_pause": 0.5,
    "page_change": 1.0,
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

    # ── Lifecycle ────────────────────────────────────────────────────

    @property
    def supported(self) -> bool:
        return self._backend is not None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def sync_with_settings(self):
        """Start or stop to match settings.ui_events_enabled."""
        if settings.ui_events_enabled and not self.running:
            self.start()
        elif not settings.ui_events_enabled and self.running:
            self.stop()

    def start(self) -> bool:
        if not self.supported:
            logger.info("UI events are not supported on this platform yet")
            return False
        if self.running:
            return True
        perms = self._backend.check_permissions()
        if not perms.accessibility:
            logger.warning("UI events: Accessibility permission missing. "
                           "Typed text will not be recorded until it is granted.")
        if not perms.input_monitoring:
            logger.warning("UI events: Input Monitoring permission missing. "
                           "Key presses will not be recorded until it is granted.")
        if not self._backend.start(self._queue):
            self._last_error = "Could not install the input hook"
            return False
        self._clip_count = self._backend.clipboard_change_count()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ui-events-enricher", daemon=True)
        self._thread.start()
        logger.info("UI event recording started")
        return True

    def stop(self):
        if not self.running:
            return
        self._backend.stop()
        self._stop.set()
        self._thread.join(timeout=3.0)
        self._thread = None
        with self._lock:
            self._emit_chunk(self._buffer.flush(), trigger=False)
            self._write_pending()
        logger.info("UI event recording stopped")

    def status(self) -> dict:
        perms = self._backend.check_permissions().as_dict() if self.supported else None
        return {
            "supported": self.supported,
            "backend": self._backend.name if self.supported else None,
            "enabled": settings.ui_events_enabled,
            "running": self.running,
            "permissions": perms,
            "events_recorded": self._events_recorded,
            "last_error": self._last_error,
        }

    def request_permissions(self) -> Optional[dict]:
        if not self.supported:
            return None
        return self._backend.request_permissions().as_dict()

    def flush_for_capture(self):
        """Write everything up to now to the DB. Called by the capture worker
        right before it links events to a new frame."""
        with self._lock:
            self._emit_chunk(self._buffer.flush(), trigger=False)
            self._write_pending()

    # ── Enricher loop ────────────────────────────────────────────────

    def _run(self):
        while not self._stop.is_set():
            try:
                raw = self._queue.get(timeout=0.25)
            except queue.Empty:
                raw = None
            try:
                with self._lock:
                    self._tick(raw, time.time())
            except Exception as e:
                self._last_error = str(e)
                logger.error(f"UI event processing failed: {e}")

    def _tick(self, raw: Optional[RawEvent], now: float):
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
        self._emit_chunk(self._buffer.poll(now), trigger=True)
        if self._pending and (
            now - self._last_db_flush >= _DB_FLUSH_S or len(self._pending) >= _DB_FLUSH_BATCH
        ):
            self._write_pending()

    # ── Gates ────────────────────────────────────────────────────────

    def _type_enabled(self, t: EventType) -> bool:
        return t.value in settings.ui_events_types_list

    def _capture_active(self) -> bool:
        cw = self._capture_worker
        return cw is None or not cw.is_paused

    def _app_blocked(self, app_name: Optional[str]) -> bool:
        return bool(app_name) and app_name.lower() in settings.blocked_apps_list

    def _should_record(self, app_name: Optional[str]) -> bool:
        return self._capture_active() and not self._app_blocked(app_name)

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
            self._emit_chunk(self._buffer.flush(), trigger=False)
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
        self._emit_chunk(self._buffer.flush(), trigger=False)
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
                      element_name=self._clean(element.name) if element else None,
                      element_value=self._clean(element.value) if element else None,
                      x=raw.x, y=raw.y, url=url)
        if not (element and element.is_text_input):
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
            self._emit_chunk(self._buffer.flush(), trigger=False)
            return
        app_name = self._app_for(target)
        if not self._should_record(app_name):
            self._emit_chunk(self._buffer.flush(), trigger=False)
            return
        window = self._front.title if self._front and self._front.app_name == app_name else None
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
        text = self._clean(text.strip()[:MAX_CLIPBOARD_CHARS])
        self._add(EventType.CLIPBOARD, now, app_name,
                  self._front.title if self._front else None, text=text)

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
            return self._clean(sanitize_url(self._backend.browser_url()))
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

    def _clean(self, text: Optional[str]) -> Optional[str]:
        if not text:
            return text
        if not settings.sensitive_filter_enabled:
            return text
        from screenmind.privacy.data_filter import filter_sensitive_text, parse_enabled_types
        return filter_sensitive_text(text, parse_enabled_types(settings.sensitive_filter_types))["clean_text"]

    def _emit_chunk(self, chunk: Optional[TextChunk], trigger: bool):
        if chunk is None:
            return
        ts = chunk.end_ts
        if chunk.is_password:
            self._add(EventType.TEXT, ts, chunk.app_name, chunk.window_title,
                      element_role=chunk.target.role, text=PASSWORD_PLACEHOLDER)
            return
        self._add(EventType.TEXT, ts, chunk.app_name, chunk.window_title,
                  element_role=chunk.target.role,
                  element_name=self._clean(chunk.target.name),
                  text=self._clean(chunk.text))
        if trigger:
            self._trigger("typing_pause")

    def _add(self, type_: EventType, ts: float, app_name, window_title, **fields):
        if fields.get("x") is not None:
            fields["x"] = int(fields["x"])
        if fields.get("y") is not None:
            fields["y"] = int(fields["y"])
        self._pending.append(UiEvent(
            timestamp=datetime.fromtimestamp(ts),
            type=type_,
            app_name=app_name,
            window_title=window_title,
            **fields,
        ))

    def _write_pending(self):
        self._last_db_flush = time.time()
        if not self._pending:
            return
        events, self._pending = self._pending, []
        if self._db is None:
            return
        try:
            self._db.insert_ui_events(events)
            self._events_recorded += len(events)
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
