"""
Abstract Base Class for Platform Adapters
All platform-specific code must implement this interface.
"""

from abc import ABC, abstractmethod
from typing import Optional, Tuple


class PlatformAdapter(ABC):
    """
    Abstract interface for platform-specific operations.
    Each OS implements this with its native APIs.
    """

    @abstractmethod
    def get_active_window_title(self) -> Optional[str]:
        """Get the title of the currently focused window."""
        ...

    @abstractmethod
    def get_active_app_name(self) -> Optional[str]:
        """Get the application/process name of the focused window."""
        ...

    @abstractmethod
    def get_foreground_window_handle(self) -> Optional[int]:
        """Get the native window handle of the focused window."""
        ...

    def get_active_window_bounds(self) -> Optional[Tuple[int, int, int, int]]:
        """Focused window as (x, y, width, height), or None if not supported."""
        return None

    def get_top_window_in(self, x: int, y: int, width: int, height: int) -> Optional[Tuple[str, Optional[str]]]:
        """(app, title) of the top window on the display at this rect.

        Used when every display is captured, so each screenshot is labeled with
        the app it shows. None means "not supported" or "no window there";
        can_find_top_window tells the two apart.
        """
        return None

    @property
    def can_find_top_window(self) -> bool:
        """True if get_top_window_in() works here, so None from it means the
        display is empty (wallpaper only), not "unknown"."""
        return False

    def is_screen_locked(self) -> bool:
        """True while the screen is locked, the screensaver runs, or another
        user has the console. Capture skips these times. Must be cheap: it
        runs once per capture tick. False if not supported."""
        return False

    def get_browser_url(self) -> Optional[str]:
        """URL of the page in the frontmost browser window. None when the
        frontmost app is not a browser or the OS does not expose it."""
        return None

    def list_visible_windows(self) -> list:
        """Every visible app window on all displays, front to back, as
        {"owner", "pid", "title", "bounds"}. Empty if not supported; callers
        then fall back to the focused window. Used for call detection."""
        return []

    def get_window_url(self, pid: Optional[int], bounds: Optional[Tuple[int, int, int, int]]) -> Optional[str]:
        """Page URL of the browser window with these bounds, or None."""
        return None

    def mic_apps(self) -> Optional[set]:
        """Lowercased names of apps capturing the microphone right now, or
        None if the OS does not tell us."""
        return None

    def get_front_window(self) -> Optional[dict]:
        """Frontmost window as {"pid", "app_name", "title"} from one consistent
        OS snapshot, or None if not supported. Used by UI event capture."""
        return None

    @abstractmethod
    def extract_a11y_text(self, hwnd: Optional[int] = None) -> Tuple[Optional[str], str]:
        """
        Extract accessible text from the foreground window.

        Args:
            hwnd: Optional window handle. If None, uses the foreground window.

        Returns:
            Tuple of (text_content, extraction_method)
            - text_content: Extracted text or None
            - extraction_method: "a11y", "none", etc.
        """
        ...

    @abstractmethod
    def is_a11y_available(self) -> bool:
        """Whether accessibility text extraction is available on this platform."""
        ...

    @property
    def platform_name(self) -> str:
        """Human-readable platform name."""
        return "Unknown"

    @property
    def trusts_os_app_name(self) -> bool:
        """Whether get_active_app_name() is a better app identity than the window title.

        False by default: on Windows the process name is an exe like "chrome.exe",
        and titles follow "<content> - <App>". Adapters whose OS reports a clean,
        user-facing app name override this to True.
        """
        return False
