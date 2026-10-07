"""A hung backend call or DB write in the UI event recorder must not stall
capture (flush_for_capture) or shutdown (stop)."""

import threading
import time
from unittest.mock import MagicMock

import pytest

from screenmind.capture.ui_events import recorder as recorder_mod
from screenmind.capture.ui_events.models import RawEvent
from screenmind.capture.ui_events.recorder import UiEventRecorder

from test_ui_events import FakeBackend, stored, type_text, ui_settings  # noqa: F401


class HangingBackend(FakeBackend):
    """front_window() hangs once `hang` is set, until `release` is set."""

    def __init__(self):
        super().__init__()
        self.hang = threading.Event()
        self.entered = threading.Event()
        self.release = threading.Event()

    def front_window(self):
        if self.hang.is_set():
            self.entered.set()
            self.release.wait(10)
        return self.front


@pytest.fixture
def fast_timeouts(monkeypatch):
    monkeypatch.setattr(recorder_mod, "_LOCK_TIMEOUT_S", 0.3)
    monkeypatch.setattr(recorder_mod, "_STOP_JOIN_S", 0.3)


def make(db=None):
    backend = HangingBackend()
    cw = MagicMock()
    cw.is_paused = False
    r = UiEventRecorder(database=db or MagicMock(), capture_worker=cw, backend=backend)
    return r, backend


def timed(fn):
    t0 = time.monotonic()
    fn()
    return time.monotonic() - t0


def test_hung_backend_call_does_not_block_flush_or_stop(ui_settings, fast_timeouts):
    db = MagicMock()
    r, b = make(db)
    # Text typed before the hang is still in the buffer.
    r._tick(None, 100.0)
    type_text(r, "draft", 100.1)
    assert r.start()
    b.hang.set()
    try:
        assert b.entered.wait(5), "enricher never reached the backend call"
        assert timed(r.flush_for_capture) < 0.2  # the lock is free: no wait at all
        assert [e.text for e in stored(db)] == ["draft"]
        assert timed(r.stop) < 2.0
        assert not r.running
    finally:
        b.release.set()


def test_hung_db_write_times_out(ui_settings, fast_timeouts):
    db = MagicMock()
    release = threading.Event()
    entered = threading.Event()

    def slow_insert(events):
        entered.set()
        release.wait(10)

    db.insert_ui_events.side_effect = slow_insert
    r, b = make(db)
    assert r.start()
    try:
        r._queue.put(RawEvent(kind="mouse_down", ts=time.time(), x=1, y=1))
        assert entered.wait(5), "enricher never wrote the click"
        # The enricher holds the lock inside the write: both calls give up.
        assert timed(r.flush_for_capture) < 2.0
        assert timed(r.stop) < 2.0
    finally:
        release.set()


def test_stuck_thread_is_not_revived_by_restart(ui_settings, fast_timeouts):
    r, b = make()
    assert r.start()
    b.hang.set()
    assert b.entered.wait(5)
    old = r._thread
    r.stop()
    b.hang.clear()
    assert r.start()
    b.release.set()
    old.join(5)
    assert not old.is_alive()
    assert r.running
    r.stop()
