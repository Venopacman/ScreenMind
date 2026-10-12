"""Capture asks for macOS Screen Recording when it first runs (F7 in
docs/plans/packaging.md), not at some random later grab."""

import asyncio
import logging
import sys
import types
from unittest.mock import MagicMock

import pytest

from screenmind.capture import window
from screenmind.platform_support.base import PlatformAdapter
from screenmind.workers.capture_worker import CaptureWorker


def _worker():
    worker = CaptureWorker(queue=asyncio.Queue(maxsize=10))
    worker._screen = MagicMock()
    worker._screen.monitors_to_capture.return_value = [None]
    worker._screen.capture.return_value = None
    return worker


@pytest.fixture
def access(monkeypatch):
    """Record each call of the check and answer with access.value."""
    state = types.SimpleNamespace(value=True, calls=[])

    def fake(request=False):
        state.calls.append(request)
        return state.value

    monkeypatch.setattr("screenmind.workers.capture_worker.screen_recording_access", fake)
    return state


class TestCaptureWorker:
    async def test_first_tick_requests_access_once(self, access):
        worker = _worker()
        await worker._capture_tick()
        await worker._capture_tick()
        assert access.calls == [True]
        assert worker.stats["screen_recording"] is True

    async def test_missing_access_logs_where_to_allow_it(self, access, caplog):
        access.value = False
        worker = _worker()
        with caplog.at_level(logging.WARNING, logger="screenmind.workers.capture_worker"):
            await worker._capture_tick()
        assert "Screen & System Audio Recording" in caplog.text
        assert worker.stats["screen_recording"] is False
        # Capture goes on: the grab still runs
        worker._screen.capture.assert_called()

    async def test_no_request_on_a_locked_screen(self, access, monkeypatch):
        monkeypatch.setattr("screenmind.workers.capture_worker.is_screen_locked", lambda: True)
        worker = _worker()
        await worker._capture_tick()
        assert access.calls == []
        assert worker.stats["screen_recording"] is None

    def test_not_checked_before_capture_runs(self, access):
        assert _worker().stats["screen_recording"] is None
        assert access.calls == []


class TestAdapters:
    def test_base_adapter_has_no_grant(self):
        assert PlatformAdapter.screen_recording_access(MagicMock()) is None

    def test_window_helper_survives_a_broken_adapter(self, monkeypatch):
        broken = MagicMock()
        broken.screen_recording_access.side_effect = RuntimeError("boom")
        monkeypatch.setattr(window, "adapter", lambda: broken)
        assert window.screen_recording_access(True) is None

    @pytest.fixture
    def quartz(self, monkeypatch):
        q = types.ModuleType("Quartz")
        q.preflight = True
        q.requested = []
        q.CGPreflightScreenCaptureAccess = lambda: q.preflight
        q.CGRequestScreenCaptureAccess = lambda: q.requested.append(1) or False
        monkeypatch.setitem(sys.modules, "Quartz", q)
        from screenmind.platform_support.macos import MacOSAdapter
        return q, MacOSAdapter.__new__(MacOSAdapter)

    def test_macos_granted_needs_no_request(self, quartz):
        q, mac = quartz
        assert mac.screen_recording_access(request=True) is True
        assert q.requested == []

    def test_macos_requests_only_when_asked(self, quartz):
        q, mac = quartz
        q.preflight = False
        assert mac.screen_recording_access() is False
        assert q.requested == []
        assert mac.screen_recording_access(request=True) is False
        assert q.requested == [1]

    def test_macos_check_failure_is_none(self, quartz):
        q, mac = quartz
        q.CGPreflightScreenCaptureAccess = MagicMock(side_effect=RuntimeError("boom"))
        assert mac.screen_recording_access(request=True) is None
