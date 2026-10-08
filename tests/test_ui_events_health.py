"""UI events: defaults, health logs and the macOS clicks-only fallback.

These cover failures that used to be silent: recorder off, no input
arriving, events lost in the hook callback, a dead hook, events skipped
while capture is paused.
"""

import logging
import queue
import sys
import threading
from unittest.mock import MagicMock

import pytest

from screenmind.capture.ui_events import recorder as recorder_mod
from screenmind.capture.ui_events.base import PermissionStatus
from screenmind.capture.ui_events.models import RawEvent
from screenmind.capture.ui_events.recorder import UiEventRecorder
from screenmind.config import Settings, settings

from test_ui_events import FakeBackend, ui_settings  # noqa: F401

LOGGER = "screenmind.capture.ui_events"


def click(ts):
    return RawEvent(kind="mouse_down", ts=ts, x=10, y=10, button="left")


class HealthBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.stats = {"keys_tapped": True, "callback_errors": 0,
                      "last_callback_error": None, "reenabled": 0}
        self.alive = True
        self.perms = PermissionStatus(True, True)
        self.start_ok = True

    def check_permissions(self):
        return self.perms

    def start(self, out):
        return self.start_ok

    def is_running(self):
        return self.alive

    def tap_stats(self):
        return dict(self.stats)


@pytest.fixture
def started(ui_settings, monkeypatch):  # noqa: F811
    """A recorder in the started state, without its enricher thread.
    The test drives _tick() itself."""
    backend = HealthBackend()
    db = MagicMock()
    cw = MagicMock()
    cw.is_paused = False
    r = UiEventRecorder(database=db, capture_worker=cw, backend=backend)
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    assert r.start()
    r._started_at = r._last_summary = 1000.0
    r._clip_count = 0
    r._tick(None, 1000.0)
    return r, backend, db, cw


def messages(caplog, level=logging.INFO):
    return [rec.getMessage() for rec in caplog.records if rec.levelno >= level]


class TestDefaults:
    def test_clicks_app_switches_text_and_clipboard_on_by_default(self):
        fields = Settings.model_fields
        assert fields["ui_events_enabled"].default is True
        assert fields["ui_events_types"].default == "click,app_switch,text,clipboard"


class TestRecorderLogs:
    def test_off_is_logged_once(self, ui_settings, monkeypatch, caplog):  # noqa: F811
        monkeypatch.setattr(settings, "ui_events_enabled", False)
        r = UiEventRecorder(database=MagicMock(), backend=HealthBackend())
        with caplog.at_level(logging.INFO, logger=LOGGER):
            r.sync_with_settings()
            r.sync_with_settings()
        off = [m for m in messages(caplog) if "UI events are off" in m]
        assert len(off) == 1

    def test_start_logs_types(self, started, caplog):
        r, b, db, cw = started
        r._thread = None
        with caplog.at_level(logging.INFO, logger=LOGGER):
            r.start()
        assert any("types: click, app_switch, text, clipboard" in m for m in messages(caplog))

    def test_missing_accessibility_is_logged(self, ui_settings, monkeypatch, caplog):  # noqa: F811
        b = HealthBackend()
        b.perms = PermissionStatus(input_monitoring=True, accessibility=False)
        r = UiEventRecorder(database=MagicMock(), backend=b)
        monkeypatch.setattr(threading.Thread, "start", lambda self: None)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            r.start()
        assert any("no Accessibility permission" in m for m in messages(caplog, logging.WARNING))

    def test_hook_failure_is_logged(self, ui_settings, caplog):  # noqa: F811
        b = HealthBackend()
        b.start_ok = False
        r = UiEventRecorder(database=MagicMock(), backend=b)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            assert r.start() is False
        assert any("could not install the input hook" in m for m in messages(caplog, logging.WARNING))
        assert r.status()["last_error"]


class TestHealth:
    def test_first_input_is_logged(self, started, caplog):
        r, b, db, cw = started
        with caplog.at_level(logging.INFO, logger=LOGGER):
            r._tick(click(1001.0), 1001.0)
            r._tick(click(1002.0), 1002.0)
        first = [m for m in messages(caplog) if "first input event" in m]
        assert first == ["UI events: first input event received (mouse_down)"]
        assert r.status()["input_events"] == 2

    def test_no_input_warns_once(self, started, caplog):
        r, b, db, cw = started
        with caplog.at_level(logging.INFO, logger=LOGGER):
            r._tick(None, 1000.0 + recorder_mod._SILENCE_WARN_S - 1)
            assert not messages(caplog, logging.WARNING)
            r._tick(None, 1000.0 + recorder_mod._SILENCE_WARN_S)
            r._tick(None, 1000.0 + recorder_mod._SILENCE_WARN_S + 1)
        warns = [m for m in messages(caplog, logging.WARNING) if "no clicks or keys" in m]
        assert len(warns) == 1

    def test_no_silence_warning_after_input(self, started, caplog):
        r, b, db, cw = started
        r._tick(click(1001.0), 1001.0)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            r._tick(None, 1000.0 + recorder_mod._SILENCE_WARN_S + 5)
        assert not [m for m in messages(caplog) if "no clicks or keys" in m]

    def test_callback_errors_are_reported(self, started, caplog):
        r, b, db, cw = started
        b.stats.update(callback_errors=3, last_callback_error="TypeError('x')")
        with caplog.at_level(logging.INFO, logger=LOGGER):
            r._tick(None, 1001.0)
            r._tick(None, 1002.0)
        lost = [m for m in messages(caplog, logging.WARNING) if "lost in the hook" in m]
        assert lost == ["UI events: 3 input events lost in the hook (last error: TypeError('x'))"]

    def test_reenabled_tap_is_reported(self, started, caplog):
        r, b, db, cw = started
        b.stats["reenabled"] = 1
        with caplog.at_level(logging.INFO, logger=LOGGER):
            r._tick(None, 1001.0)
        assert any("turned the input hook off 1 time" in m for m in messages(caplog))

    def test_dead_hook_warns_once(self, started, caplog):
        r, b, db, cw = started
        b.alive = False
        with caplog.at_level(logging.INFO, logger=LOGGER):
            r._tick(None, 1001.0)
            r._tick(None, 1002.0)
        dead = [m for m in messages(caplog, logging.WARNING) if "input hook stopped" in m]
        assert len(dead) == 1
        assert r.status()["hook_running"] is False

    def test_paused_capture_is_counted(self, started):
        r, b, db, cw = started
        cw.is_paused = True
        r._tick(click(1001.0), 1001.0)
        assert r.status()["skipped"] == {"capture paused": 1}
        db.insert_ui_events.assert_not_called()

    def test_summary_lists_stored_and_skipped(self, started, caplog):
        r, b, db, cw = started
        r._tick(click(1001.0), 1001.0)
        r._tick(None, 1004.0)  # DB flush
        cw.is_paused = True
        r._tick(click(1005.0), 1005.0)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            r._tick(None, 1000.0 + recorder_mod._SUMMARY_S)
        summary = [m for m in messages(caplog) if m.startswith("UI events, last")]
        assert summary == ["UI events, last 10 min: 2 clicks/keys in, stored click 1; skipped: 1 (capture paused)"]

    def test_quiet_period_logs_no_summary(self, started, caplog):
        r, b, db, cw = started
        with caplog.at_level(logging.INFO, logger=LOGGER):
            r._tick(None, 1000.0 + recorder_mod._SUMMARY_S)
        assert not [m for m in messages(caplog) if m.startswith("UI events, last")]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS backend")
class TestMacOSTapFallback:
    def _backend(self, results):
        from screenmind.capture.ui_events.macos import MacOSUiEventBackend
        b = MacOSUiEventBackend.__new__(MacOSUiEventBackend)
        q = MagicMock()
        q.kCGEventLeftMouseDown, q.kCGEventRightMouseDown, q.kCGEventKeyDown = 1, 3, 10
        q.CGEventTapCreate.side_effect = list(results)
        b._Q = q
        b._tap = None
        b._run_loop = None
        b._out = queue.SimpleQueue()
        b._callback_errors = 0
        b._last_callback_error = None
        b._reenabled = 0
        b._keys_tapped = False
        return b, q

    def _run(self, b):
        result = {"ok": False}
        b._run_tap(threading.Event(), result)
        return result["ok"]

    def test_full_tap(self):
        b, q = self._backend([object()])
        assert self._run(b)
        assert b.tap_stats()["keys_tapped"] is True
        assert q.CGEventTapCreate.call_count == 1

    def test_clicks_only_without_input_monitoring(self, caplog):
        b, q = self._backend([None, object()])
        with caplog.at_level(logging.INFO, logger="screenmind.capture.ui_events.macos"):
            assert self._run(b)
        assert b.tap_stats()["keys_tapped"] is False
        mouse_mask = (1 << 1) | (1 << 3)
        assert q.CGEventTapCreate.call_args_list[1].args[3] == mouse_mask
        assert any("Recording clicks only" in m for m in messages(caplog, logging.WARNING))

    def test_no_tap_at_all(self, caplog):
        b, q = self._backend([None, None])
        with caplog.at_level(logging.INFO, logger="screenmind.capture.ui_events.macos"):
            assert not self._run(b)
        assert any("Nothing will be recorded" in m for m in messages(caplog, logging.WARNING))

    def test_callback_error_is_counted(self):
        b, q = self._backend([])
        q.CGEventGetLocation.side_effect = RuntimeError("boom")
        event = object()
        assert b._tap_callback(None, q.kCGEventLeftMouseDown, event, None) is event
        assert b.tap_stats()["callback_errors"] == 1
        assert "boom" in b.tap_stats()["last_callback_error"]
