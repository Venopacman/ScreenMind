"""Tray icon: status, pause or resume, open dashboard, quit.

This module holds the menu and the status text. It needs no OS API, so tests
run it with a fake backend. The Win32 part is
screenmind/platform_support/win_tray.py. main.py imports this module only on
Windows and only when settings.tray_icon_on is true: always in the app,
from source only with TRAY_ICON=true. macOS needs NSStatusItem on the main
thread; that is a later milestone. See docs/plans/packaging.md (Install details).

The menu acts on the workers directly, in the same way as the HTTP routes:
pause and resume like /api/capture/pause and /api/capture/resume, quit
through the same request_shutdown as /api/shutdown.
"""

import logging
from pathlib import Path
from typing import Callable, List, NamedTuple, Optional

logger = logging.getLogger("screenmind.tray")

APP_NAME = "ScreenMind"
# How often the backend reads the status, so a pause from the dashboard or an
# auto-pause shows in the tray. One cheap check per poll.
POLL_S = 2.0
TOOLTIP_MAX = 127  # NOTIFYICONDATAW.szTip holds 128 characters with the NUL

ASSETS = Path(__file__).parent / "assets"
ICON_ON = ASSETS / "favicon.ico"
ICON_OFF = ASSETS / "tray-paused.ico"  # the same icon, grey and faded


class MenuItem(NamedTuple):
    label: str
    action: Optional[Callable[[], None]] = None
    enabled: bool = True


SEPARATOR = None


class TrayController:
    """Status and menu actions. The backend calls these on its own thread."""

    def __init__(self, capture_worker, request_shutdown: Optional[Callable[[], None]],
                 dashboard_url: Optional[str] = None,
                 open_url: Optional[Callable[[str], object]] = None):
        self._capture = capture_worker
        self._request_shutdown = request_shutdown
        self._dashboard_url = dashboard_url
        self._open_url = open_url
        self.quitting = False

    # ── Status ───────────────────────────────────────────────────────
    def state(self) -> str:
        """'recording', 'paused', 'incognito', 'auto_paused' or 'stopped'."""
        cap = self._capture
        if cap is None:
            return "stopped"
        if getattr(cap, "incognito", False):
            return "incognito"
        if cap.is_paused:
            return "paused"
        if getattr(cap, "auto_paused_for", None):
            return "auto_paused"
        return "recording"

    def status_text(self) -> str:
        """Lower-case status, e.g. 'recording' or 'paused while game.exe runs'."""
        state = self.state()
        if state == "auto_paused":
            return f"paused while {self._capture.auto_paused_for} runs"
        if state == "stopped":
            return "not running"
        return state

    def tooltip(self) -> str:
        return f"{APP_NAME} — {self.status_text()}"[:TOOLTIP_MAX]

    def is_recording(self) -> bool:
        """The backend shows the normal icon when true, the grey one otherwise."""
        return self.state() == "recording"

    # ── Menu ─────────────────────────────────────────────────────────
    def menu(self) -> List[Optional[MenuItem]]:
        """Built fresh each time the menu opens, so it shows the current state."""
        status = self.status_text()
        items: List[Optional[MenuItem]] = [
            MenuItem(status[:1].upper() + status[1:], None, enabled=False),
            SEPARATOR,
        ]
        if self._capture is not None:
            if self._capture.is_paused:
                items.append(MenuItem("Resume capture", self.resume))
            else:
                items.append(MenuItem("Pause capture", self.pause))
        if self._dashboard_url:
            items.append(MenuItem("Open dashboard", self.open_dashboard))
        items += [SEPARATOR, MenuItem("Quit ScreenMind", self.quit, enabled=not self.quitting)]
        return items

    # ── Actions ──────────────────────────────────────────────────────
    def pause(self):
        if self._capture is not None:
            self._capture.pause(source="tray")

    def resume(self):
        """Resume, and leave incognito as the dashboard's toggle does."""
        cap = self._capture
        if cap is None:
            return
        if getattr(cap, "incognito", False):
            cap.incognito = False
        cap.resume(source="tray")

    def open_dashboard(self):
        if not self._dashboard_url:
            return
        opener = self._open_url
        if opener is None:
            import webbrowser
            opener = webbrowser.open
        opener(self._dashboard_url)

    def quit(self):
        """The same clean stop as POST /api/shutdown."""
        if self.quitting:
            return
        self.quitting = True
        logger.info("Quit from the tray icon")
        if self._request_shutdown is not None:
            self._request_shutdown()


def start_tray(capture_worker, request_shutdown, dashboard_url: Optional[str] = None):
    """Show the tray icon on its own thread. Returns the backend (call .stop()),
    or None when it could not start. Windows only."""
    from screenmind.platform_support.win_tray import Win32Tray

    controller = TrayController(capture_worker, request_shutdown, dashboard_url)
    tray = Win32Tray(controller, icon_on=ICON_ON, icon_off=ICON_OFF, poll_s=POLL_S)
    if not tray.start():
        logger.warning("Tray icon did not start; running without it")
        tray.stop()
        return None
    return tray
