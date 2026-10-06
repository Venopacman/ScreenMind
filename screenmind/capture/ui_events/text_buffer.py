"""
Groups single key presses into text chunks, one chunk per focused field.

Pure Python with no OS calls, so it is easy to test. The recorder feeds it
key events plus the element that had focus, and stores each flushed chunk
as one "text" UI event.
"""

from dataclasses import dataclass
from typing import List, Optional

from screenmind.capture.ui_events.models import (
    ElementInfo,
    KEY_ARROW,
    KEY_BACKSPACE,
    KEY_CHAR,
    KEY_ENTER,
    KEY_ESCAPE,
    KEY_TAB,
    RawEvent,
)

# Keys that end a chunk. After them we can no longer tell where the cursor is,
# or the user has finished the input (Enter sends a chat message, etc.).
_FLUSH_KEYS = {KEY_ENTER, KEY_TAB, KEY_ARROW, KEY_ESCAPE}


@dataclass
class TextChunk:
    text: str
    target: ElementInfo
    app_name: Optional[str]
    window_title: Optional[str]
    start_ts: float
    end_ts: float
    is_password: bool = False


class TextBuffer:
    def __init__(self, idle_flush_s: float = 2.0, max_chars: int = 500):
        self.idle_flush_s = idle_flush_s
        self.max_chars = max_chars
        self._chars: List[str] = []
        self._target: Optional[ElementInfo] = None
        self._app: Optional[str] = None
        self._window: Optional[str] = None
        self._start_ts = 0.0
        self._last_ts = 0.0
        self._typed_any = False  # password chunks keep no chars but still count

    @property
    def active(self) -> bool:
        return self._target is not None

    @property
    def target(self) -> Optional[ElementInfo]:
        return self._target

    def add(
        self,
        raw: RawEvent,
        target: ElementInfo,
        app_name: Optional[str],
        window_title: Optional[str],
    ) -> List[TextChunk]:
        """Feed one key event. Returns any chunks that were completed by it."""
        out: List[TextChunk] = []

        if raw.shortcut:
            # Cmd+V, Cmd+Z, Cmd+A... change the text in ways we cannot follow.
            self._flush_into(out)
            return out

        if self.active and self._target.identity() != target.identity():
            self._flush_into(out)

        if raw.key in _FLUSH_KEYS:
            self._flush_into(out)
            return out

        if raw.key == KEY_BACKSPACE:
            if self._chars:
                self._chars.pop()
                self._last_ts = raw.ts
            return out

        if raw.key != KEY_CHAR or not raw.char:
            return out

        if not self.active:
            self._target = target
            self._app = app_name
            self._window = window_title
            self._start_ts = raw.ts
            self._typed_any = False

        self._typed_any = True
        self._last_ts = raw.ts
        if not target.is_password:
            self._chars.append(raw.char)
            if len(self._chars) >= self.max_chars:
                self._flush_into(out)
        return out

    def poll(self, now: float) -> Optional[TextChunk]:
        """Flush the current chunk if the user stopped typing."""
        if self.active and now - self._last_ts >= self.idle_flush_s:
            return self.flush()
        return None

    def flush(self) -> Optional[TextChunk]:
        out: List[TextChunk] = []
        self._flush_into(out)
        return out[0] if out else None

    def _flush_into(self, out: List[TextChunk]):
        if not self.active:
            return
        target = self._target
        text = "".join(self._chars).strip()
        chunk = None
        if target.is_password:
            if self._typed_any:
                chunk = TextChunk("", target, self._app, self._window,
                                  self._start_ts, self._last_ts, is_password=True)
        elif text:
            chunk = TextChunk(text, target, self._app, self._window,
                              self._start_ts, self._last_ts)
        self._chars = []
        self._target = None
        self._typed_any = False
        if chunk:
            out.append(chunk)
