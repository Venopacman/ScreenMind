"""
Windows Platform Adapter
Uses Win32 APIs (ctypes) for window detection and UI Automation for a11y.
"""

import logging
import os
import sys
import time
from typing import Optional, Tuple

from screenmind.platform_support.base import PlatformAdapter

logger = logging.getLogger("screenmind.platform_support.windows")

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_kernel32 = None
_user32 = None


def _dlls():
    """Private WinDLL handles with argtypes set. Kept apart from ctypes.windll so
    our argtypes never clash with other packages (keyboard, mss)."""
    global _kernel32, _user32
    if _kernel32 is None:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        _kernel32, _user32 = k, u
    return _kernel32, _user32


def process_name(pid: int) -> Optional[str]:
    """Executable name without extension ("chrome", "notepad"), or None.

    Uses PROCESS_QUERY_LIMITED_INFORMATION, which also works for elevated
    processes. This name is what blocked_apps matches against.
    """
    if not pid:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        kernel32, _ = _dlls()
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return None
            return os.path.splitext(os.path.basename(buf.value))[0] or None
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


class WindowsAdapter(PlatformAdapter):
    """Windows implementation using ctypes + UI Automation."""

    def __init__(self):
        self._a11y_initialized = False
        self._uia = None
        self._a11y_available = True

    @property
    def platform_name(self) -> str:
        return "Windows"

    def get_foreground_window_handle(self) -> Optional[int]:
        """Get the foreground window handle using Win32 API."""
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            return hwnd if hwnd else None
        except Exception:
            return None

    def get_active_window_title(self) -> Optional[str]:
        """Get window title using Win32 GetWindowTextW."""
        try:
            import ctypes

            hwnd = ctypes.windll.user32.GetForegroundWindow()
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return None
            buf = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
            return buf.value if buf.value else None
        except Exception:
            return None

    def get_active_app_name(self) -> Optional[str]:
        """Get process name via Win32 APIs."""
        try:
            import ctypes
            from ctypes import wintypes

            _, user32 = _dlls()
            hwnd = user32.GetForegroundWindow()
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return process_name(pid.value)
        except Exception:
            return None

    def get_front_window(self) -> Optional[dict]:
        """Foreground window as {"pid", "app_name", "title"}, all from one HWND."""
        try:
            import ctypes
            from ctypes import wintypes

            _, user32 = _dlls()
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return None
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            title = None
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value or None
            return {"pid": pid.value or None, "app_name": process_name(pid.value), "title": title}
        except Exception:
            return None

    # ── Accessibility ────────────────────────────────────────────────

    def _ensure_a11y_init(self):
        """Lazy-init the UI Automation client."""
        if self._a11y_initialized:
            return
        self._a11y_initialized = True

        try:
            import uiautomation as auto
            self._uia = auto
            logger.debug("Windows UI Automation initialized")
        except ImportError:
            try:
                self._uia = None
                logger.warning("uiautomation not available, trying ctypes fallback")
            except Exception:
                self._a11y_available = False
                logger.warning("Accessibility API not available on this system")

    def is_a11y_available(self) -> bool:
        return self._a11y_available

    def extract_a11y_text(self, hwnd: Optional[int] = None) -> Tuple[Optional[str], str]:
        """Extract text using Windows UI Automation or ctypes fallback."""
        if not self._a11y_available:
            return None, "none"

        self._ensure_a11y_init()

        try:
            start = time.time()

            if self._uia:
                text = self._extract_uiautomation(hwnd)
            else:
                text = self._extract_ctypes(hwnd)

            elapsed = time.time() - start

            if text and len(text.strip()) > 20:
                lines = [l for l in text.strip().split('\n') if l.strip()]
                logger.debug(f"Extracted {len(lines)} text elements in {elapsed:.2f}s")
                return text.strip(), "a11y"
            else:
                return None, "none"

        except Exception as e:
            logger.error(f"Extraction failed: {e}")
            return None, "none"

    def _extract_uiautomation(self, hwnd: Optional[int] = None) -> Optional[str]:
        """Extract text using the uiautomation library."""
        auto = self._uia

        try:
            if hwnd:
                window = auto.ControlFromHandle(hwnd)
            else:
                window = auto.GetForegroundControl()

            if not window:
                return None

            texts = []
            self._walk_tree(window, texts, depth=0, max_depth=8)

            return '\n'.join(texts) if texts else None

        except Exception:
            return None

    def _walk_tree(self, control, texts: list, depth: int, max_depth: int = 8):
        """Recursively walk the UI Automation tree and extract text."""
        if depth > max_depth:
            return

        try:
            name = control.Name
            if name and name.strip() and len(name.strip()) > 1:
                text = name.strip()
                if text not in texts[-5:] if texts else True:
                    texts.append(text)

            try:
                value = control.GetValuePattern().Value
                if value and value.strip() and value.strip() != name:
                    texts.append(value.strip())
            except Exception:
                pass

            children = control.GetChildren()
            if children:
                for child in children:
                    self._walk_tree(child, texts, depth + 1, max_depth)
                    if len(texts) > 500:
                        return

        except Exception:
            pass

    def _extract_ctypes(self, hwnd: Optional[int] = None) -> Optional[str]:
        """Fallback: Extract text using raw Win32 APIs via ctypes."""
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32

            if hwnd is None:
                hwnd = user32.GetForegroundWindow()

            if not hwnd:
                return None

            texts = []

            WNDENUMPROC = ctypes.WINFUNCTYPE(
                wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
            )

            def enum_callback(child_hwnd, _lparam):
                length = user32.GetWindowTextLengthW(child_hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(child_hwnd, buf, length + 1)
                    text = buf.value.strip()
                    if text and len(text) > 1:
                        texts.append(text)
                return len(texts) < 300

            user32.EnumChildWindows(hwnd, WNDENUMPROC(enum_callback), 0)

            return '\n'.join(texts) if texts else None

        except Exception:
            return None
