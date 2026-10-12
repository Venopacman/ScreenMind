"""
Platform Abstraction Layer
Auto-detects the OS and provides the correct adapter for:
  - Window title detection
  - App name detection
  - Accessibility text extraction
  - Foreground window handle

Usage:
    from evolve_capture.platform_support import get_adapter
    adapter = get_adapter()
    title = adapter.get_active_window_title()
    app = adapter.get_active_app_name()
    text, method = adapter.extract_a11y_text()
"""

import os
import sys

from evolve_capture.platform_support.base import PlatformAdapter


def is_wayland() -> bool:
    """Detect Wayland session. Checks both XDG_SESSION_TYPE and WAYLAND_DISPLAY."""
    if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        return True
    if os.environ.get("WAYLAND_DISPLAY"):
        return True
    return False


def get_adapter() -> PlatformAdapter:
    """Get the platform adapter for the current OS."""
    if sys.platform == "win32":
        from evolve_capture.platform_support.windows import WindowsAdapter
        return WindowsAdapter()
    elif sys.platform == "darwin":
        from evolve_capture.platform_support.macos import MacOSAdapter
        return MacOSAdapter()
    else:
        from evolve_capture.platform_support.linux import LinuxAdapter
        return LinuxAdapter()


# Convenience: singleton for common use
_adapter = None


def adapter() -> PlatformAdapter:
    """Get or create the singleton platform adapter."""
    global _adapter
    if _adapter is None:
        _adapter = get_adapter()
    return _adapter
