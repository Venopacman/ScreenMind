"""Tray icon: status text, menu actions, when it turns on, and the Win32 backend.

The controller tests run on any OS with a fake capture worker. The backend
tests need Windows; they show the icon for a moment and never open the menu.
"""

import ctypes
import sys
import time
from unittest.mock import MagicMock

import pytest

from screenmind import config
from screenmind.tray import ICON_OFF, ICON_ON, TOOLTIP_MAX, TrayController


class FakeCapture:
    def __init__(self, paused=False, incognito=False, heavy=None):
        self._paused = paused
        self.incognito = incognito
        self.auto_paused_for = heavy
        self.calls = []

    @property
    def is_paused(self):
        return self._paused

    def pause(self, source="unknown"):
        self.calls.append(("pause", source))
        self._paused = True

    def resume(self, source="unknown"):
        self.calls.append(("resume", source))
        self._paused = False


def _labels(menu):
    return [None if i is None else i.label for i in menu]


def _item(menu, label):
    return next(i for i in menu if i is not None and i.label == label)


# ── Status ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("cap, state, tip", [
    (FakeCapture(), "recording", "ScreenMind — recording"),
    (FakeCapture(paused=True), "paused", "ScreenMind — paused"),
    (FakeCapture(paused=True, incognito=True), "incognito", "ScreenMind — incognito"),
    (FakeCapture(heavy="game.exe"), "auto_paused", "ScreenMind — paused while game.exe runs"),
    (None, "stopped", "ScreenMind — not running"),
])
def test_status_and_tooltip(cap, state, tip):
    c = TrayController(cap, request_shutdown=None)
    assert c.state() == state
    assert c.tooltip() == tip
    assert c.is_recording() is (state == "recording")


def test_tooltip_fits_the_shell_limit():
    c = TrayController(FakeCapture(heavy="x" * 300), request_shutdown=None)
    assert len(c.tooltip()) == TOOLTIP_MAX


def test_status_follows_a_pause_from_elsewhere():
    cap = FakeCapture()
    c = TrayController(cap, request_shutdown=None)
    assert c.state() == "recording"
    cap.pause(source="dashboard")
    assert c.state() == "paused"
    assert "Resume capture" in _labels(c.menu())


# ── Menu ────────────────────────────────────────────────────────────


def test_menu_while_recording():
    c = TrayController(FakeCapture(), MagicMock(), dashboard_url="http://127.0.0.1:7792")
    menu = c.menu()
    assert _labels(menu) == ["Recording", None, "Pause capture", "Open dashboard", None,
                             "Quit ScreenMind"]
    status = menu[0]
    assert status.enabled is False and status.action is None


def test_menu_without_dashboard_url_has_no_open_item():
    c = TrayController(FakeCapture(paused=True), MagicMock())
    assert _labels(c.menu()) == ["Paused", None, "Resume capture", None, "Quit ScreenMind"]


def test_pause_and_resume_call_the_worker():
    cap = FakeCapture()
    c = TrayController(cap, MagicMock())
    _item(c.menu(), "Pause capture").action()
    _item(c.menu(), "Resume capture").action()
    assert cap.calls == [("pause", "tray"), ("resume", "tray")]


def test_resume_leaves_incognito():
    cap = FakeCapture(paused=True, incognito=True)
    c = TrayController(cap, MagicMock())
    assert c.menu()[0].label == "Incognito"
    _item(c.menu(), "Resume capture").action()
    assert cap.incognito is False
    assert c.state() == "recording"


def test_open_dashboard_opens_the_url():
    opened = []
    c = TrayController(FakeCapture(), MagicMock(), dashboard_url="http://127.0.0.1:7792",
                       open_url=opened.append)
    _item(c.menu(), "Open dashboard").action()
    assert opened == ["http://127.0.0.1:7792"]


def test_quit_uses_the_shutdown_path_once():
    shutdown = MagicMock()
    c = TrayController(FakeCapture(), shutdown)
    _item(c.menu(), "Quit ScreenMind").action()
    c.quit()
    shutdown.assert_called_once_with()
    assert _item(c.menu(), "Quit ScreenMind").enabled is False


def test_icons_ship_in_the_package():
    assert ICON_ON.is_file() and ICON_OFF.is_file()


# ── When the tray turns on ──────────────────────────────────────────


def test_off_from_source_by_default(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert config.Settings(_env_file=None).tray_icon_on is False


def test_on_in_the_app(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert config.Settings(_env_file=None).tray_icon_on is True


def test_env_overrides_both_ways(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv("TRAY_ICON", "true")
    assert config.Settings(_env_file=None).tray_icon_on is True
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("TRAY_ICON", "false")
    assert config.Settings(_env_file=None).tray_icon_on is False


def test_main_does_not_import_the_tray_at_module_level():
    import subprocess
    code = ("import sys, screenmind.main; "
            "print(any(m in sys.modules for m in ('screenmind.tray', "
            "'screenmind.platform_support.win_tray')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.stdout.strip() == "False", out.stderr
    # The only import sits behind the Windows check
    import screenmind.main
    source = open(screenmind.main.__file__, encoding="utf-8").read()
    assert 'sys.platform == "win32" and settings.tray_icon_on' in source


# ── Capture worker: auto-pause for a heavy app ──────────────────────


def test_auto_paused_for(monkeypatch):
    from screenmind.workers.capture_worker import CaptureWorker
    w = CaptureWorker.__new__(CaptureWorker)
    w._paused = False
    w._heavy_app_logged = "game.exe"
    monkeypatch.setattr(config.settings, "auto_pause_heavy_apps", True)
    assert w.auto_paused_for == "game.exe"
    monkeypatch.setattr(config.settings, "auto_pause_heavy_apps", False)
    assert w.auto_paused_for is None
    monkeypatch.setattr(config.settings, "auto_pause_heavy_apps", True)
    w._paused = True
    assert w.auto_paused_for is None
    w._paused = False
    w._heavy_app_logged = None
    assert w.auto_paused_for is None


# ── Win32 backend (Windows only) ────────────────────────────────────

win_only = pytest.mark.skipif(sys.platform != "win32", reason="Win32 tray backend")


def _wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def _find_tray_window():
    from ctypes import wintypes as W
    find = ctypes.WinDLL("user32").FindWindowW
    find.restype = W.HWND
    find.argtypes = [W.LPCWSTR, W.LPCWSTR]
    from screenmind.platform_support.win_tray import WINDOW_CLASS
    return find(WINDOW_CLASS, None)


@win_only
def test_win32_icon_shows_follows_status_and_goes_away():
    from screenmind.platform_support import win_tray

    cap = FakeCapture()
    tray = win_tray.Win32Tray(TrayController(cap, MagicMock()), ICON_ON, ICON_OFF)
    assert tray.start() is True
    try:
        assert all(tray._icons), "both .ico files load"
        assert _wait(lambda: tray._added)
        assert tray._shown == ("ScreenMind — recording", True)
        assert _find_tray_window() == tray._hwnd

        cap.pause(source="dashboard")
        tray.refresh()
        assert _wait(lambda: tray._shown == ("ScreenMind — paused", False))
    finally:
        tray.stop()
    assert not tray._thread.is_alive()
    assert tray._added is False
    assert not _find_tray_window()


@win_only
def test_win32_menu_runs_the_chosen_action(monkeypatch):
    """Builds the real menu, but a fake TrackPopupMenu picks the item, so no
    menu appears on screen."""
    from screenmind.platform_support import win_tray

    cap = FakeCapture()
    controller = TrayController(cap, MagicMock())
    labels = _labels(controller.menu())
    pick = labels.index("Pause capture") + 1  # command ids start at 1
    monkeypatch.setattr(win_tray, "_TrackPopupMenu", lambda *a: pick)
    monkeypatch.setattr(win_tray, "_SetForegroundWindow", lambda *a: True)

    tray = win_tray.Win32Tray(controller, ICON_ON, ICON_OFF)
    assert tray.start() is True
    try:
        assert _wait(lambda: tray._added)
        win_tray._PostMessageW(tray._hwnd, win_tray.WM_TRAY, win_tray.ICON_ID, win_tray.WM_RBUTTONUP)
        assert _wait(lambda: cap.calls == [("pause", "tray")])
        assert _wait(lambda: tray._shown == ("ScreenMind — paused", False))
    finally:
        tray.stop()


@win_only
def test_win32_start_reports_a_window_failure(monkeypatch):
    from screenmind.platform_support import win_tray

    monkeypatch.setattr(win_tray, "_CreateWindowExW", lambda *a: None)
    tray = win_tray.Win32Tray(TrayController(FakeCapture(), MagicMock()), ICON_ON, ICON_OFF)
    assert tray.start() is False
    tray.stop()
    assert tray._hwnd is None
    monkeypatch.undo()
    # The window class was unregistered, so a later start works
    tray = win_tray.Win32Tray(TrayController(FakeCapture(), MagicMock()), ICON_ON, ICON_OFF)
    assert tray.start() is True
    tray.stop()
