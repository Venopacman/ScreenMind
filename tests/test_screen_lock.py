"""Capture skips a locked screen or a running screensaver (macOS).

2026-10-08 14:09-14:20: both displays saved a frame every ~10 s while the
screen was locked. The lock screen showed a landscape, the window list still
had Claude and Chrome on top, and Gemma described "a landscape image".
"""

import asyncio
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from screenmind.capture.ui_events.models import ALL_EVENT_TYPES, describe_event
from screenmind.workers.capture_worker import CaptureWorker


def _worker(db=None):
    worker = CaptureWorker(queue=asyncio.Queue(maxsize=10), database=db)
    worker._screen = MagicMock()
    worker._screen.monitors_to_capture.return_value = [None]
    worker._screen.capture.return_value = None
    return worker


def _set_locked(monkeypatch, value):
    monkeypatch.setattr("screenmind.workers.capture_worker.is_screen_locked", lambda: value)


class TestCaptureWorker:
    async def test_locked_screen_grabs_nothing(self, monkeypatch):
        _set_locked(monkeypatch, True)
        worker = _worker()
        with patch("screenmind.workers.capture_worker.get_active_app_name") as app:
            await worker._capture_tick(trigger="periodic")
            await worker._capture_tick(trigger="click")
        worker._screen.capture.assert_not_called()
        app.assert_not_called()
        assert worker._queue.empty()
        assert worker.stats["screen_locked"] is True

    async def test_capture_resumes_after_unlock(self, monkeypatch):
        _set_locked(monkeypatch, True)
        worker = _worker()
        await worker._capture_tick()
        _set_locked(monkeypatch, False)
        with patch("screenmind.workers.capture_worker.get_active_app_name", return_value="Claude"), \
             patch("screenmind.workers.capture_worker.get_active_window_title", return_value="Claude"):
            await worker._capture_tick()
        worker._screen.capture.assert_called_once()
        assert worker.stats["screen_locked"] is False

    async def test_one_marker_per_lock_and_unlock(self, db, monkeypatch):
        worker = _worker(db)
        for locked in (False, True, True, True, False, False):
            _set_locked(monkeypatch, locked)
            await worker._capture_tick()
        rows = db.get_ui_events_range(datetime(2000, 1, 1), datetime(2100, 1, 1))
        assert [r["type"] for r in rows] == ["screen_locked", "screen_unlocked"]
        assert all(r["app_name"] is None and r["activity_id"] is None for r in rows)

    async def test_lock_logged_once(self, monkeypatch, caplog):
        _set_locked(monkeypatch, True)
        worker = _worker()
        with caplog.at_level("INFO", logger="screenmind.workers.capture_worker"):
            for _ in range(3):
                await worker._capture_tick()
        assert sum("Screen locked" in r.message for r in caplog.records) == 1

    async def test_events_on_the_lock_screen_are_not_linked(self, monkeypatch):
        """A frame after unlock links only events from after the unlock."""
        worker = _worker()
        worker._link_floor["focused"] = datetime(2026, 10, 8, 14, 9)
        _set_locked(monkeypatch, True)
        await worker._capture_tick()
        _set_locked(monkeypatch, False)
        before = datetime.now()
        await worker._capture_tick()
        assert worker._link_floor == {}
        assert worker._started_at >= before


class TestMarkerEventType:
    def test_markers_are_not_recordable_input_types(self):
        assert "screen_locked" not in ALL_EVENT_TYPES
        assert "screen_unlocked" not in ALL_EVENT_TYPES
        assert "click" in ALL_EVENT_TYPES

    def test_describe(self):
        assert describe_event("screen_locked", None, None, None, None, None) == "screen locked"
        assert describe_event("screen_unlocked", None, None, None, None, None) == "screen unlocked"


class TestWindowHelper:
    def test_failing_adapter_never_stops_capture(self):
        from screenmind.capture import window

        broken = MagicMock()
        broken.is_screen_locked.side_effect = RuntimeError("boom")
        with patch.object(window, "adapter", return_value=broken):
            assert window.is_screen_locked() is False

    def test_base_adapter_says_unlocked(self):
        from screenmind.platform_support.base import PlatformAdapter
        assert PlatformAdapter.is_screen_locked(MagicMock()) is False


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS adapter")
class TestMacOSAdapter:
    @pytest.fixture
    def adapter(self):
        from screenmind.platform_support.macos import MacOSAdapter
        return MacOSAdapter()

    @pytest.fixture
    def quartz(self, monkeypatch):
        Quartz = pytest.importorskip("Quartz")
        state = {"session": {"kCGSSessionOnConsoleKey": True}, "idle": 0.0, "windows": []}
        monkeypatch.setattr(Quartz, "CGSessionCopyCurrentDictionary", lambda: state["session"])
        monkeypatch.setattr(Quartz, "CGEventSourceSecondsSinceLastEventType",
                            lambda *a: state["idle"])
        scans = []

        def window_list(*a):
            scans.append(a)
            return state["windows"]

        monkeypatch.setattr(Quartz, "CGWindowListCopyWindowInfo", window_list)
        state["scans"] = scans
        return state

    @staticmethod
    def _win(owner, layer, w=1512, h=982):
        return {"kCGWindowOwnerName": owner, "kCGWindowLayer": layer,
                "kCGWindowBounds": {"X": 0, "Y": 0, "Width": w, "Height": h}}

    def test_unlocked_active_user_skips_window_scan(self, adapter, quartz):
        assert adapter.is_screen_locked() is False
        assert quartz["scans"] == []

    def test_lock_key(self, adapter, quartz):
        quartz["session"]["CGSSessionScreenIsLocked"] = True
        assert adapter.is_screen_locked() is True

    def test_other_user_on_console(self, adapter, quartz):
        quartz["session"]["kCGSSessionOnConsoleKey"] = False
        assert adapter.is_screen_locked() is True

    def test_screensaver_window_when_idle(self, adapter, quartz):
        quartz["idle"] = 120.0
        quartz["windows"] = [self._win("Google Chrome", 0), self._win("ScreenSaverEngine", 1000)]
        assert adapter.is_screen_locked() is True

    def test_large_window_above_screensaver_level(self, adapter, quartz):
        quartz["idle"] = 120.0
        quartz["windows"] = [self._win("loginwindow", 2002)]
        assert adapter.is_screen_locked() is True

    def test_idle_without_screensaver(self, adapter, quartz):
        quartz["idle"] = 120.0
        quartz["windows"] = [self._win("Google Chrome", 0), self._win("Control Centre", 25, 40, 30)]
        assert adapter.is_screen_locked() is False

    def test_small_high_window_is_not_a_screensaver(self, adapter, quartz):
        quartz["idle"] = 120.0
        quartz["windows"] = [self._win("Some HUD", 2000, 200, 100)]
        assert adapter.is_screen_locked() is False

    def test_quartz_error_means_unlocked(self, adapter, quartz, monkeypatch):
        import Quartz

        def boom():
            raise RuntimeError("no session")

        monkeypatch.setattr(Quartz, "CGSessionCopyCurrentDictionary", boom)
        assert adapter.is_screen_locked() is False
