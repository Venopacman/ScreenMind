"""
Active Window Title Capture
Reads the currently focused window title using OS-native APIs.
Provides ground truth for app detection alongside Gemma 4 vision analysis.

This module now delegates to the platform_support adapter for cross-platform
compatibility. The API remains identical for backward compatibility.
"""

from typing import Optional, Tuple

from screenmind.platform_support import adapter


def get_active_window_title() -> Optional[str]:
    """
    Get the title of the currently active (focused) window.

    Returns:
        Window title string, or None if detection fails.
    """
    try:
        return adapter().get_active_window_title()
    except Exception:
        return None


def get_active_app_name() -> Optional[str]:
    """
    Get the application/process name of the active window.

    Returns:
        App name string, or None if detection fails.
    """
    try:
        return adapter().get_active_app_name()
    except Exception:
        return None


def can_find_top_window() -> bool:
    """Whether get_top_window_in() is supported on this OS."""
    try:
        return adapter().can_find_top_window
    except Exception:
        return False


def get_top_window_in(monitor: dict) -> Optional[Tuple[str, Optional[str]]]:
    """
    Get (app name, window title) of the top window on one display.

    Args:
        monitor: mss monitor dict (left, top, width, height).

    Returns:
        (app, title) tuple, or None if unsupported or no window found.
    """
    try:
        return adapter().get_top_window_in(
            monitor["left"], monitor["top"], monitor["width"], monitor["height"]
        )
    except Exception:
        return None
