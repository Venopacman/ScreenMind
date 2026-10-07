"""
Screen Capture Module
Captures screenshots using mss (fastest cross-platform method).
On Wayland, delegates to WaylandScreenCapture (grim / XDG Portal).
Saves as JPEG with configurable quality to date-organized directories.
"""

import logging
import io
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import mss
import mss.tools
from PIL import Image

from screenmind.config import settings
from screenmind.platform_support import is_wayland

logger = logging.getLogger("screenmind.capture.screen")

# macOS: a CoreGraphics grab (what mss uses) can hang for 30 s. We saw it in
# every process started from a Claude session, with Screen Recording granted.
# The system serializes screen capture, so one hung grab also stalls other apps
# that capture (the main instance). After one slow grab, this process uses the
# `screencapture` tool instead, which takes about 0.2 s there.
_SLOW_GRAB_SECONDS = 5.0


def _grab_screencapture(monitor: dict) -> Optional[Image.Image]:
    """macOS: grab a display rect (global points) with the screencapture tool.
    None on any failure, so the caller can fall back to mss."""
    rect = f"{monitor['left']},{monitor['top']},{monitor['width']},{monitor['height']}"
    with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
        try:
            subprocess.run(
                ["screencapture", "-x", "-t", "png", "-R", rect, tmp.name],
                check=True, capture_output=True, timeout=10,
            )
            with Image.open(tmp.name) as img:
                return img.convert("RGB")
        except Exception as e:
            logger.debug(f"screencapture failed: {e}")
            return None


class ScreenCapture:
    """Handles screenshot capture, compression, and storage.

    On Wayland Linux, delegates to WaylandScreenCapture (grim/XDG Portal).
    On X11/Windows/macOS, uses mss directly.
    """

    def __init__(self):
        self._backend = None
        self._sct = None
        self._use_screencapture = False

        if sys.platform == "linux" and is_wayland():
            try:
                from screenmind.capture.wayland import WaylandScreenCapture
                self._backend = WaylandScreenCapture()
            except Exception as e:
                logger.error(f"Wayland backend failed to init: {e}")
                logger.warning("Capture will be unavailable.")
                # self._backend stays None — capture() returns None gracefully
        else:
            self._sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()

    def _get_active_monitor(self) -> dict:
        """Return mss monitor dict for the monitor containing the active window.

        Platform-specific detection:
        - Windows: MonitorFromWindow API (handles DPI, spanning, minimized)
        - Linux X11: xdotool getwindowgeometry (center-point match)
        - macOS: Quartz CGWindowListCopyWindowInfo (center-point match)
        - Linux Wayland: Falls back to primary (Wayland hides window positions)

        Falls back to monitors[1] (primary) on any error.
        """
        monitors = self._sct.monitors
        if len(monitors) <= 2:
            return monitors[1]  # single monitor, nothing to detect

        try:
            if sys.platform == "win32":
                return self._detect_monitor_win32(monitors)

            # Linux X11 / macOS: get window center and match against monitors
            center = self._get_window_center()
            if center:
                cx, cy = center
                for mon in monitors[1:]:
                    if (mon["left"] <= cx < mon["left"] + mon["width"]
                            and mon["top"] <= cy < mon["top"] + mon["height"]):
                        logger.debug(
                            "Active monitor detected: %dx%d at (%d, %d)",
                            mon["width"], mon["height"],
                            mon["left"], mon["top"],
                        )
                        return mon
                logger.debug(
                    "Active window center (%d, %d) not in any monitor", cx, cy
                )
        except Exception as exc:
            logger.debug("Active monitor detection failed: %s", exc)

        return monitors[1]  # ultimate fallback

    # ── Platform-specific helpers ────────────────────────────────────────

    def _detect_monitor_win32(self, monitors: list) -> dict:
        """Windows: use MonitorFromWindow + GetMonitorInfoW."""
        import ctypes
        from ctypes import wintypes

        MONITOR_DEFAULTTOPRIMARY = 1
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        hmonitor = ctypes.windll.user32.MonitorFromWindow(
            hwnd, MONITOR_DEFAULTTOPRIMARY
        )

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not ctypes.windll.user32.GetMonitorInfoW(
            hmonitor, ctypes.byref(mi)
        ):
            return monitors[1]

        target_left = mi.rcMonitor.left
        target_top = mi.rcMonitor.top

        for mon in monitors[1:]:
            if mon["left"] == target_left and mon["top"] == target_top:
                logger.debug(
                    "Active monitor detected: %dx%d at (%d, %d)",
                    mon["width"], mon["height"],
                    mon["left"], mon["top"],
                )
                return mon

        logger.debug("Active monitor not matched in mss list, using primary")
        return monitors[1]

    def _get_window_center(self) -> Optional[Tuple[int, int]]:
        """Get the center point (x, y) of the active window.

        Returns None if detection is unavailable (e.g. Wayland).
        """
        if sys.platform == "darwin":
            return self._window_center_macos()
        elif sys.platform == "linux":
            return self._window_center_linux()
        return None

    def _window_center_linux(self) -> Optional[Tuple[int, int]]:
        """Linux X11: use xdotool to get active window geometry."""
        import subprocess

        result = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowgeometry", "--shell"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode != 0:
            return None

        # Output format: WINDOW=...\nX=...\nY=...\nWIDTH=...\nHEIGHT=...
        vals = {}
        for line in result.stdout.strip().splitlines():
            if "=" in line:
                key, val = line.split("=", 1)
                vals[key] = int(val)

        if "X" in vals and "Y" in vals and "WIDTH" in vals and "HEIGHT" in vals:
            cx = vals["X"] + vals["WIDTH"] // 2
            cy = vals["Y"] + vals["HEIGHT"] // 2
            return cx, cy
        return None

    def _window_center_macos(self) -> Optional[Tuple[int, int]]:
        """macOS: center of the frontmost window, from the platform adapter."""
        from screenmind.platform_support import adapter

        bounds = adapter().get_active_window_bounds()
        if not bounds:
            return None
        x, y, w, h = bounds
        return x + w // 2, y + h // 2

    def active_monitor(self) -> Optional[dict]:
        """The mss monitor holding the focused window, or None on Wayland."""
        if not self._sct:
            return None
        return self._get_active_monitor()

    def monitors_to_capture(self) -> list:
        """Monitors to grab this tick.

        Every display when CAPTURE_ALL_MONITORS is on and there is more than one.
        Otherwise [None], which means "let capture() pick one" (primary, or the
        active one with CAPTURE_ACTIVE_MONITOR). Wayland always gets [None].
        """
        if self._sct and settings.capture_all_monitors and len(self._sct.monitors) > 2:
            return list(self._sct.monitors[1:])
        return [None]

    def _select_monitor(self) -> dict:
        """Pick the monitor to capture based on settings."""
        if settings.capture_active_monitor:
            mon = self._get_active_monitor()
        else:
            mon = self._sct.monitors[1]
        self._last_monitor_key = f"{mon['left']},{mon['top']}"
        return mon

    @property
    def last_monitor_key(self) -> Optional[str]:
        """Key identifying the last captured monitor (e.g. '0,0'), or None."""
        return getattr(self, "_last_monitor_key", None)

    def capture(self, monitor: Optional[dict] = None) -> Optional[Tuple[Path, Image.Image]]:
        """
        Capture a screenshot and save as a compressed JPEG.

        Args:
            monitor: mss monitor dict from monitors_to_capture(). None picks
                     one by settings (primary, or active if Beta enabled).

        Returns:
            Tuple of (saved file path, PIL Image) or None if capture fails.
        """
        if self._backend:
            return self._backend.capture()

        if self._sct:
            try:
                suffix = ""
                if monitor is None:
                    monitor = self._select_monitor()
                else:
                    self._last_monitor_key = f"{monitor['left']},{monitor['top']}"
                    # Displays grabbed in one tick can share a millisecond
                    suffix = f"_m{self._sct.monitors.index(monitor)}"
                img = self._grab(monitor)

                # Save to date-organized directory
                now = datetime.now()
                date_dir = settings.screenshots_dir / now.strftime("%Y-%m-%d")
                date_dir.mkdir(parents=True, exist_ok=True)

                filename = f"{now.strftime('%H-%M-%S')}_{int(now.timestamp() * 1000) % 1000:03d}{suffix}.jpg"
                filepath = date_dir / filename

                img.save(
                    str(filepath),
                    "JPEG",
                    quality=settings.screenshot_quality,
                    optimize=True,
                )

                # Encrypt at rest if enabled (no-op when encryption_enabled=False)
                try:
                    from screenmind.privacy.encryption import encrypt_image
                    encrypt_image(filepath)
                except Exception:
                    pass  # Never fail capture due to encryption

                return filepath, img

            except Exception as e:
                logger.error(f"Error capturing screenshot: {e}")
                return None

        return None  # both _backend and _sct are None (init failure)

    def _grab(self, monitor: dict) -> Image.Image:
        """One display as an RGB image."""
        if self._use_screencapture:
            img = _grab_screencapture(monitor)
            if img is not None:
                return img
        start = time.monotonic()
        raw = self._sct.grab(monitor)
        if sys.platform == "darwin" and time.monotonic() - start > _SLOW_GRAB_SECONDS:
            logger.warning(
                "Screen grab took %.0fs; using the screencapture tool from now on",
                time.monotonic() - start,
            )
            self._use_screencapture = True
        # mss returns BGRA, PIL expects RGB
        return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

    def capture_to_bytes(self) -> Optional[Tuple[bytes, Image.Image]]:
        """
        Capture screenshot and return as JPEG bytes (for immediate processing
        without saving to disk first).

        Returns:
            Tuple of (JPEG bytes, PIL Image) or None if capture fails.
        """
        if self._backend:
            return self._backend.capture_to_bytes()

        if self._sct:
            try:
                monitor = self._select_monitor()
                img = self._grab(monitor)

                buffer = io.BytesIO()
                img.save(
                    buffer,
                    "JPEG",
                    quality=settings.screenshot_quality,
                    optimize=True,
                )
                return buffer.getvalue(), img

            except Exception as e:
                logger.error(f"Error capturing to bytes: {e}")
                return None

        return None

    def close(self):
        """Release capture resources."""
        if self._backend:
            self._backend.close()
        elif self._sct:
            self._sct.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

