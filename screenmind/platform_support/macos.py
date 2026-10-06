"""
macOS Platform Adapter
Uses AppKit/NSWorkspace for window detection.
Accessibility via AXUIElement (requires accessibility permission).
"""

import logging
import subprocess
from typing import Optional, Tuple

from screenmind.platform_support.base import PlatformAdapter

logger = logging.getLogger("screenmind.platform_support.macos")

# Apps whose AX tree has a web area with the page URL (AXURL).
BROWSER_APPS = {
    "google chrome", "google chrome canary", "chromium", "safari", "safari technology preview",
    "arc", "brave browser", "microsoft edge", "vivaldi", "opera", "orion", "firefox", "zen",
}

# Subtrees that are app chrome, not window content. Chrome can expose the
# whole menu bar many times over ("File | Edit | View | ...").
_A11Y_SKIP_ROLES = {"AXMenuBar", "AXMenuBarItem", "AXMenu", "AXMenuItem"}
# Text areas (Terminal, editors) hold the whole buffer in AXValue. Above this
# size we read only the visible part.
_A11Y_VISIBLE_ONLY_CHARS = 4000
_A11Y_MAX_TOTAL_CHARS = 20000


class MacOSAdapter(PlatformAdapter):
    """macOS implementation using AppKit (pyobjc) and AXUIElement."""

    def __init__(self):
        self._appkit_available = False
        self._ax_available = False
        self._init_frameworks()

        self._manual_a11y_pids: set = set()

    def _init_frameworks(self):
        """Try to import macOS frameworks."""
        try:
            from AppKit import NSWorkspace  # type: ignore
            self._appkit_available = True
            logger.debug("macOS AppKit initialized")
        except ImportError:
            logger.warning("macOS AppKit not available (install pyobjc: pip install pyobjc-framework-Cocoa)")

        try:
            from ApplicationServices import (  # type: ignore
                AXUIElementCreateSystemWide,
                AXUIElementCopyAttributeValue,
            )
            self._ax_available = True
            logger.debug("macOS Accessibility initialized")
        except ImportError:
            logger.warning("macOS Accessibility not available (install pyobjc-framework-ApplicationServices)")

    @property
    def platform_name(self) -> str:
        return "macOS"

    def _front_window(self, within: Optional[Tuple[int, int, int, int]] = None) -> Optional[dict]:
        """Return the frontmost normal window from Quartz (owner, pid, title, bounds).

        NSWorkspace.frontmostApplication() goes stale in a process without an
        NSRunLoop, so we read the live on-screen window list instead. It is
        ordered front to back; layer 0 is the normal app window layer.
        kCGWindowName needs Screen Recording permission, else it is empty.

        With `within` (x, y, width, height), only windows whose center lies in
        that rect count. This finds the top window on one display.
        """
        try:
            import Quartz  # type: ignore
            windows = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
                Quartz.kCGNullWindowID,
            ) or []
            for w in windows:
                if w.get("kCGWindowLayer", 1) != 0:
                    continue
                bounds = w.get("kCGWindowBounds") or {}
                if bounds.get("Width", 0) < 50 or bounds.get("Height", 0) < 50:
                    continue
                if within:
                    cx = bounds.get("X", 0) + bounds["Width"] / 2
                    cy = bounds.get("Y", 0) + bounds["Height"] / 2
                    rx, ry, rw, rh = within
                    if not (rx <= cx < rx + rw and ry <= cy < ry + rh):
                        continue
                return {
                    "owner": w.get("kCGWindowOwnerName"),
                    "pid": w.get("kCGWindowOwnerPID"),
                    "title": w.get("kCGWindowName") or None,
                    "bounds": (
                        int(bounds.get("X", 0)), int(bounds.get("Y", 0)),
                        int(bounds["Width"]), int(bounds["Height"]),
                    ),
                }
        except Exception as e:
            logger.debug(f"Quartz window lookup failed: {e}")
        return None

    def get_foreground_window_handle(self) -> Optional[int]:
        """macOS doesn't use integer window handles like Win32. Returns PID instead."""
        front = self._front_window()
        return front["pid"] if front else None

    def get_active_window_title(self) -> Optional[str]:
        """Get the frontmost window's title, falling back to the app name.

        Electron apps (Claude, Slack...) often title every window with just the
        app name. Then the page title of the window's web area is the real
        context (e.g. the conversation name), so we use that instead.
        """
        front = self._front_window()
        if not front:
            return None
        return self._best_title(front)

    def _best_title(self, win: dict) -> Optional[str]:
        """The Quartz title, or the web area title when Quartz only gives
        the app name (Electron apps), or the app name as a last resort."""
        title = win["title"]
        if not title or title == win["owner"]:
            title = self._ax_document_title(win["pid"], win.get("bounds")) or title
        return title or win["owner"]

    def get_active_app_name(self) -> Optional[str]:
        """Get the app that owns the frontmost window."""
        front = self._front_window()
        return front["owner"] if front else None

    def get_active_window_bounds(self) -> Optional[Tuple[int, int, int, int]]:
        """Frontmost window as (x, y, width, height) in global screen points."""
        front = self._front_window()
        return front["bounds"] if front else None

    def get_top_window_in(self, x: int, y: int, width: int, height: int) -> Optional[Tuple[str, Optional[str]]]:
        """(app, title) of the top window on the display at this rect."""
        win = self._front_window(within=(x, y, width, height))
        if not win or not win["owner"]:
            return None
        return win["owner"], self._best_title(win)

    @property
    def can_find_top_window(self) -> bool:
        return True

    def get_front_window(self) -> Optional[dict]:
        """Frontmost window as {"pid", "app_name", "title"}."""
        front = self._front_window()
        if not front:
            return None
        return {"pid": front["pid"], "app_name": front["owner"], "title": front["title"]}

    def get_browser_url(self) -> Optional[str]:
        """URL of the page in the frontmost browser window, or None if the
        frontmost app is not a browser or the URL is not exposed."""
        front = self._front_window()
        if not front or (front["owner"] or "").lower() not in BROWSER_APPS:
            return None
        window = self._ax_focused_window(front["pid"])
        if window is None:
            return None
        web_area = self._ax_find_role(window, "AXWebArea")
        if web_area is not None:
            url = self._ax_attr(web_area, "AXURL")
            if url:
                url = str(url)
                if url.startswith(("http://", "https://", "file://")):
                    return url
        return None

    # ── AX helpers ───────────────────────────────────────────────────

    def _ax_attr(self, element, attr):
        try:
            from ApplicationServices import AXUIElementCopyAttributeValue  # type: ignore
            err, value = AXUIElementCopyAttributeValue(element, attr, None)
            return value if err == 0 else None
        except Exception:
            return None

    def enable_full_a11y_tree(self, pid: Optional[int]):
        """Ask Electron/Chromium apps to build their full AX tree. Without it,
        their web content shows up as empty groups. Done once per process;
        apps that do not know the attribute just return an error."""
        if not pid or pid in self._manual_a11y_pids or not self._ax_available:
            return
        self._manual_a11y_pids.add(pid)
        try:
            from ApplicationServices import (  # type: ignore
                AXUIElementCreateApplication, AXUIElementSetAttributeValue,
            )
            AXUIElementSetAttributeValue(AXUIElementCreateApplication(pid), "AXManualAccessibility", True)
        except Exception:
            pass

    def _ax_focused_window(self, pid: Optional[int]):
        if not pid or not self._ax_available:
            return None
        try:
            from ApplicationServices import AXUIElementCreateApplication  # type: ignore
            app = AXUIElementCreateApplication(pid)
        except Exception:
            return None
        window = self._ax_attr(app, "AXFocusedWindow")
        if window is None:
            windows = self._ax_attr(app, "AXWindows") or []
            window = windows[0] if len(windows) else None
        return window

    def _ax_find_role(self, element, role: str, max_nodes: int = 400):
        """Breadth-first search for the first element with this role."""
        queue = [element]
        seen = 0
        while queue and seen < max_nodes:
            el = queue.pop(0)
            seen += 1
            if self._ax_attr(el, "AXRole") == role:
                return el
            children = self._ax_attr(el, "AXChildren") or []
            queue.extend(children)
        return None

    def _ax_window_at(self, pid: Optional[int], bounds: Tuple[int, int, int, int]):
        """The app's AX window with these Quartz bounds (x, y, w, h).

        An app can have windows on several displays; its focused window may
        not be the one we are labeling. Falls back to the focused window only
        when the app has a single window.
        """
        if not pid or not self._ax_available:
            return None
        try:
            from ApplicationServices import (  # type: ignore
                AXUIElementCreateApplication, AXValueGetValue,
                kAXValueCGPointType, kAXValueCGSizeType,
            )
            windows = self._ax_attr(AXUIElementCreateApplication(pid), "AXWindows") or []
            x, y, w, h = bounds
            for win in windows:
                ok_p, pos = AXValueGetValue(self._ax_attr(win, "AXPosition"), kAXValueCGPointType, None)
                ok_s, size = AXValueGetValue(self._ax_attr(win, "AXSize"), kAXValueCGSizeType, None)
                if ok_p and ok_s and abs(pos.x - x) <= 4 and abs(pos.y - y) <= 4 \
                        and abs(size.width - w) <= 4 and abs(size.height - h) <= 4:
                    return win
            if len(windows) == 1:
                return windows[0]
        except Exception:
            pass
        return None

    def _ax_document_title(self, pid: Optional[int],
                           bounds: Optional[Tuple[int, int, int, int]] = None) -> Optional[str]:
        """Title of the first web area that has one, in the window with these
        bounds (or the focused window when no bounds are given)."""
        self.enable_full_a11y_tree(pid)
        window = self._ax_window_at(pid, bounds) if bounds else self._ax_focused_window(pid)
        if window is None:
            return None
        queue = [window]
        seen = 0
        while queue and seen < 400:
            el = queue.pop(0)
            seen += 1
            if self._ax_attr(el, "AXRole") == "AXWebArea":
                title = self._ax_attr(el, "AXTitle")
                if isinstance(title, str) and title.strip():
                    return title.strip()
            queue.extend(self._ax_attr(el, "AXChildren") or [])
        return None

    # ── Accessibility ────────────────────────────────────────────────

    def is_a11y_available(self) -> bool:
        return self._ax_available

    @property
    def trusts_os_app_name(self) -> bool:
        """The window owner name ("Terminal", "Google Chrome") is user-facing, while
        titles rarely end with the app name (Terminal: "dir — cmd — 120×30")."""
        return True

    def extract_a11y_text(self, hwnd: Optional[int] = None) -> Tuple[Optional[str], str]:
        """
        Extract accessible text using macOS Accessibility API (AXUIElement).
        Requires user to grant accessibility permission in System Preferences.
        """
        if not self._ax_available:
            return None, "none"

        try:
            import Quartz  # type: ignore
            from ApplicationServices import (  # type: ignore
                AXUIElementCreateApplication,
                AXUIElementCopyAttributeValue,
                kAXFocusedWindowAttribute,
                kAXChildrenAttribute,
                kAXValueAttribute,
                kAXTitleAttribute,
                kAXRoleAttribute,
            )

            # Get focused app PID
            pid = hwnd or self.get_foreground_window_handle()
            if not pid:
                return None, "none"

            self.enable_full_a11y_tree(pid)
            app_ref = AXUIElementCreateApplication(pid)

            # Get focused window
            err, window = AXUIElementCopyAttributeValue(app_ref, kAXFocusedWindowAttribute, None)
            if err or not window:
                return None, "none"

            texts = []
            self._walk_ax_tree(window, texts, depth=0, max_depth=8, seen=set(), budget=[_A11Y_MAX_TOTAL_CHARS])

            if texts:
                result = '\n'.join(texts)
                if len(result.strip()) > 20:
                    logger.debug(f"macOS: Extracted {len(texts)} elements")
                    return result.strip(), "a11y"

            return None, "none"

        except Exception as e:
            logger.error(f"macOS extraction failed: {e}")
            return None, "none"

    def _walk_ax_tree(self, element, texts: list, depth: int, max_depth: int = 8,
                      seen: Optional[set] = None, budget: Optional[list] = None):
        """Walk the AXUIElement tree to extract text.

        Skips menu subtrees, drops repeated lines, reads only the visible part
        of large text areas, and stops after _A11Y_MAX_TOTAL_CHARS.
        """
        seen = set() if seen is None else seen
        budget = [_A11Y_MAX_TOTAL_CHARS] if budget is None else budget
        if depth > max_depth or len(texts) > 500 or budget[0] <= 0:
            return

        try:
            role = self._ax_attr(element, "AXRole")
            if role in _A11Y_SKIP_ROLES:
                return

            def _add(text: str):
                text = text.strip()
                if len(text) <= 1 or text in seen:
                    return
                seen.add(text)
                text = text[:budget[0]]
                budget[0] -= len(text)
                texts.append(text)

            title = self._ax_attr(element, "AXTitle")
            if title and str(title).strip():
                _add(str(title))

            value = self._ax_attr(element, "AXValue")
            if isinstance(value, str) and value.strip():
                if len(value) > _A11Y_VISIBLE_ONLY_CHARS:
                    value = self._ax_visible_text(element) or value[-_A11Y_VISIBLE_ONLY_CHARS:]
                if value.strip() != str(title or "").strip():
                    _add(value)

            children = self._ax_attr(element, "AXChildren")
            if children:
                for child in children:
                    self._walk_ax_tree(child, texts, depth + 1, max_depth, seen, budget)

        except Exception:
            pass

    def _ax_visible_text(self, element) -> Optional[str]:
        """Only the on-screen part of a text area (e.g. Terminal without its
        scrollback), via AXVisibleCharacterRange + AXStringForRange."""
        try:
            from ApplicationServices import AXUIElementCopyParameterizedAttributeValue  # type: ignore
            visible = self._ax_attr(element, "AXVisibleCharacterRange")
            if visible is None:
                return None
            err, text = AXUIElementCopyParameterizedAttributeValue(element, "AXStringForRange", visible, None)
            return str(text) if err == 0 and text else None
        except Exception:
            return None
