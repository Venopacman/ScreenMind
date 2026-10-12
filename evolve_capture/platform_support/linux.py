"""
Linux Platform Adapter
Uses xdotool/wmctrl for window detection on X11.
On Wayland, uses compositor-specific IPC (swaymsg/hyprctl/niri msg).
AT-SPI for accessibility on both.
"""

import logging
import json
import os
import subprocess
import time
from typing import Optional, Tuple

from evolve_capture.platform_support.base import PlatformAdapter

logger = logging.getLogger("evolve_capture.platform_support.linux")


def _is_wayland() -> bool:
    """Detect Wayland session. Inlined here to avoid importing from the
    parent package (platform_support/__init__.py), which would create a
    fragile circular-import hazard if __init__.py ever adds a top-level
    import of this module."""
    if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        return True
    if os.environ.get("WAYLAND_DISPLAY"):
        return True
    return False


class LinuxAdapter(PlatformAdapter):
    """Linux implementation using xdotool/wmctrl + AT-SPI.

    On Wayland, falls back to compositor IPC for window title/app detection.
    """

    def __init__(self):
        self._atspi_available = False
        self._xdotool_available = False
        self._compositor = None  # safe default for X11
        self._is_wayland = _is_wayland()
        if self._is_wayland:
            self._compositor = self._detect_compositor()
        self._cached_focused = None      # {"title": ..., "app": ...}
        self._cached_focused_ts = 0.0    # timestamp of last fetch
        self._check_tools()

    def _check_tools(self):
        """Check which tools are available."""
        try:
            result = subprocess.run(
                ["xdotool", "--version"],
                capture_output=True, timeout=3
            )
            self._xdotool_available = result.returncode == 0
            if self._xdotool_available:
                logger.info("Linux xdotool available")
        except Exception:
            logger.warning("xdotool not found (install: sudo apt install xdotool)")

        try:
            import pyatspi  # type: ignore
            self._atspi_available = True
            logger.info("Linux AT-SPI available")
        except ImportError:
            logger.warning("AT-SPI not available (install: pip install pyatspi)")

    @property
    def platform_name(self) -> str:
        return "Linux"

    def get_foreground_window_handle(self) -> Optional[int]:
        """Get the X11 window ID using xdotool.

        Note: X11-only. Returns None on Wayland (no equivalent concept —
        Wayland has no global window IDs). The AT-SPI a11y code ignores
        this handle anyway (walks desktop for STATE_ACTIVE).
        """
        if not self._xdotool_available:
            return None
        try:
            result = subprocess.run(
                ["xdotool", "getactivewindow"],
                capture_output=True, text=True, timeout=2
            )
            if result.returncode == 0:
                return int(result.stdout.strip())
        except Exception:
            pass
        return None

    def get_active_window_title(self) -> Optional[str]:
        """Get active window title. Uses compositor IPC on Wayland, xdotool on X11."""
        if self._is_wayland:
            return self._get_wayland_window_info("title")

        if not self._xdotool_available:
            return self._get_title_xprop()

        try:
            result = subprocess.run(
                ["xdotool", "getactivewindow", "getwindowname"],
                capture_output=True, text=True, timeout=2
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except Exception:
            return None

    def get_active_app_name(self) -> Optional[str]:
        """Get app name. Uses compositor IPC on Wayland, xdotool+/proc on X11."""
        if self._is_wayland:
            return self._get_wayland_window_info("app")

        if not self._xdotool_available:
            title = self.get_active_window_title()
            if title:
                parts = title.rsplit(" - ", 1)
                return parts[-1] if parts else title
            return None

        try:
            # Get PID of active window
            result = subprocess.run(
                ["xdotool", "getactivewindow", "getwindowpid"],
                capture_output=True, text=True, timeout=2
            )
            if result.returncode == 0:
                pid = result.stdout.strip()
                # Read process name from /proc
                comm_path = f"/proc/{pid}/comm"
                if os.path.exists(comm_path):
                    with open(comm_path) as f:
                        return f.read().strip()
        except Exception:
            pass

        # Fallback: extract from title
        title = self.get_active_window_title()
        if title:
            parts = title.rsplit(" - ", 1)
            return parts[-1] if parts else title
        return None

    def _get_title_xprop(self) -> Optional[str]:
        """Fallback: get window title using xprop."""
        try:
            result = subprocess.run(
                ["xprop", "-root", "_NET_ACTIVE_WINDOW"],
                capture_output=True, text=True, timeout=2
            )
            if result.returncode != 0:
                return None

            # Parse window ID
            parts = result.stdout.strip().split()
            if not parts:
                return None
            win_id = parts[-1]

            # Get window name
            result2 = subprocess.run(
                ["xprop", "-id", win_id, "WM_NAME"],
                capture_output=True, text=True, timeout=2
            )
            if result2.returncode == 0:
                # Output: WM_NAME(STRING) = "title"
                import re
                match = re.search(r'"(.+)"', result2.stdout)
                if match:
                    return match.group(1)
        except Exception:
            pass
        return None

    # ── Wayland compositor IPC ───────────────────────────────────────

    def _detect_compositor(self) -> Optional[str]:
        """Detect which Wayland compositor is running."""
        if os.environ.get("SWAYSOCK"):
            return "sway"
        if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            return "hyprland"
        if os.environ.get("NIRI_SOCKET"):
            return "niri"
        # GNOME/KDE: no reliable env var that provides window IPC
        return None

    def _get_wayland_window_info(self, field: str) -> Optional[str]:
        """Get window title or app name from compositor IPC.
        Caches the focused node for 1 second to avoid redundant subprocess calls.
        """
        now = time.time()
        if now - self._cached_focused_ts > 1.0:
            self._cached_focused = self._fetch_focused_window()
            self._cached_focused_ts = now

        if self._cached_focused:
            return self._cached_focused.get(field)
        return None

    def _fetch_focused_window(self) -> Optional[dict]:
        """Fetch focused window metadata from compositor.
        Returns {"title": ..., "app": ...} or None.
        """
        try:
            if self._compositor == "sway":
                return self._sway_focused()
            elif self._compositor == "hyprland":
                return self._hyprland_focused()
            elif self._compositor == "niri":
                return self._niri_focused()
        except Exception:
            pass
        return None

    def _sway_focused(self) -> Optional[dict]:
        """Get focused window from swaymsg -t get_tree."""
        result = subprocess.run(
            ["swaymsg", "-t", "get_tree"],
            capture_output=True, text=True, timeout=2
        )
        if result.returncode != 0:
            return None

        tree = json.loads(result.stdout)

        def find_focused(node):
            if node.get("focused"):
                return node
            for child in node.get("nodes", []) + node.get("floating_nodes", []):
                found = find_focused(child)
                if found:
                    return found
            return None

        focused = find_focused(tree)
        if focused:
            return {
                "title": focused.get("name"),
                "app": focused.get("app_id") or focused.get("window_properties", {}).get("class"),
            }
        return None

    def _hyprland_focused(self) -> Optional[dict]:
        """Get focused window from hyprctl activewindow -j."""
        result = subprocess.run(
            ["hyprctl", "activewindow", "-j"],
            capture_output=True, text=True, timeout=2
        )
        if result.returncode != 0:
            return None

        data = json.loads(result.stdout)
        return {
            "title": data.get("title"),
            "app": data.get("class") or data.get("initialClass"),
        }

    def _niri_focused(self) -> Optional[dict]:
        """Get focused window from niri msg --json focused-window."""
        result = subprocess.run(
            ["niri", "msg", "--json", "focused-window"],
            capture_output=True, text=True, timeout=2
        )
        if result.returncode != 0:
            return None

        data = json.loads(result.stdout)
        if not data or not isinstance(data, dict):
            return None
        return {
            "title": data.get("title"),
            "app": data.get("app_id"),
        }

    # ── Accessibility ────────────────────────────────────────────────

    def is_a11y_available(self) -> bool:
        return self._atspi_available

    def extract_a11y_text(self, hwnd: Optional[int] = None) -> Tuple[Optional[str], str]:
        """Extract text using AT-SPI (Linux accessibility framework)."""
        if not self._atspi_available:
            return None, "none"

        try:
            import pyatspi  # type: ignore

            desktop = pyatspi.Registry.getDesktop(0)

            # Find the active application
            active_app = None
            for app in desktop:
                try:
                    state = app.getState()
                    if state.contains(pyatspi.STATE_ACTIVE):
                        active_app = app
                        break
                except Exception:
                    continue

            if not active_app:
                # Fallback: use the first app with a focused window
                for app in desktop:
                    try:
                        for i in range(app.childCount):
                            child = app.getChildAtIndex(i)
                            if child and child.getState().contains(pyatspi.STATE_FOCUSED):
                                active_app = app
                                break
                    except Exception:
                        continue
                    if active_app:
                        break

            if not active_app:
                return None, "none"

            texts = []
            self._walk_atspi_tree(active_app, texts, depth=0, max_depth=8)

            if texts:
                result = '\n'.join(texts)
                if len(result.strip()) > 20:
                    logger.debug(f"Linux: Extracted {len(texts)} elements")
                    return result.strip(), "a11y"

            return None, "none"

        except Exception as e:
            logger.error(f"Linux extraction failed: {e}")
            return None, "none"

    def _walk_atspi_tree(self, node, texts: list, depth: int, max_depth: int = 8):
        """Walk the AT-SPI tree to extract text."""
        if depth > max_depth or len(texts) > 500:
            return

        try:
            import pyatspi  # type: ignore
            # Get name
            name = node.name
            if name and name.strip() and len(name.strip()) > 1:
                if name.strip() not in texts[-5:]:
                    texts.append(name.strip())

            # Get text content (for text widgets)
            try:
                text_iface = node.queryText()
                if text_iface:
                    full_text = text_iface.getText(0, text_iface.characterCount)
                    if full_text and full_text.strip() and full_text.strip() != name:
                        texts.append(full_text.strip())
            except Exception:
                pass

            # Recurse
            for i in range(node.childCount):
                try:
                    child = node.getChildAtIndex(i)
                    if child:
                        self._walk_atspi_tree(child, texts, depth + 1, max_depth)
                except Exception:
                    pass

        except Exception:
            pass
