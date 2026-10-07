"""
Windows Platform Adapter
Uses Win32 APIs (ctypes) for window detection and UI Automation for a11y.
"""

import logging
import os
import re
import sys
import threading
import time
from typing import Optional, Tuple

from screenmind.platform_support.base import PlatformAdapter

logger = logging.getLogger("screenmind.platform_support.windows")

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_kernel32 = None
_user32 = None

try:
    import ctypes as _ctypes
    from ctypes import wintypes as _wt
    _WNDENUMPROC = getattr(_ctypes, "WINFUNCTYPE", _ctypes.CFUNCTYPE)(_wt.BOOL, _wt.HWND, _wt.LPARAM)
except Exception:  # pragma: no cover - non-Windows without wintypes
    _WNDENUMPROC = None


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
        u.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
        u.EnumChildWindows.argtypes = [wintypes.HWND, _WNDENUMPROC, wintypes.LPARAM]
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsIconic.argtypes = [wintypes.HWND]
        u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        u.GetWindowLongW.restype = wintypes.LONG
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        _kernel32, _user32 = k, u
    return _kernel32, _user32


def _is_cloaked(hwnd) -> bool:
    """DWM-cloaked windows exist but are not shown (other virtual desktops,
    suspended UWP apps)."""
    import ctypes
    from ctypes import wintypes
    try:
        cloaked = wintypes.DWORD()
        ctypes.WinDLL("dwmapi").DwmGetWindowAttribute(
            wintypes.HWND(hwnd), _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        return bool(cloaked.value)
    except Exception:
        return False


def _window_text(hwnd) -> str:
    import ctypes
    _, user32 = _dlls()
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _window_pid(hwnd) -> int:
    """Process that owns a window. UWP apps (Calculator, Settings) draw inside
    an ApplicationFrameHost frame; for those, the app's own process."""
    import ctypes
    from ctypes import wintypes
    _, user32 = _dlls()
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if (process_name(pid.value) or "").lower() != "applicationframehost":
        return pid.value
    found = []

    def _child(child, _):
        cpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(child, ctypes.byref(cpid))
        if cpid.value != pid.value:
            found.append(cpid.value)
            return False
        return True

    user32.EnumChildWindows(hwnd, _WNDENUMPROC(_child), 0)
    return found[0] if found else pid.value


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


# ── UI Automation helpers ────────────────────────────────────────────

_UIA_TIMEOUT_MS = 1000  # UIA's default lets a hung app block a call for 20 s
_UIA_ValueValuePropertyId = 30045
_UIA_ValueIsReadOnlyPropertyId = 30046
_UIA_ControlTypePropertyId = 30003
_UIA_NamePropertyId = 30005
_UIA_IsPasswordPropertyId = 30019
_UIA_IsOffscreenPropertyId = 30022
_UIA_AriaRolePropertyId = 30101
_UIA_DocumentControlTypeId = 50030
_TreeScope_Descendants = 4
_TreeScope_Subtree = 7

_uia_tls = threading.local()
_uia_timeouts_set = False


class ComInit:
    """COM set up for one thread, released when the object goes away.

    Like uiautomation.UIAutomationInitializerInThread, but quiet when it is
    freed at interpreter exit, after comtypes has been torn down (that is
    when the main thread's thread-locals go).
    """

    def __init__(self, auto):
        self._auto = auto
        auto.InitializeUIAutomationInCurrentThread()

    def __del__(self):
        try:
            self._auto.UninitializeUIAutomationInCurrentThread()
        except Exception:
            pass


def uia():
    """The uiautomation module, with COM set up for the calling thread.

    COM is initialized lazily, once per thread; the initializer lives in a
    thread-local, so it is released on that thread when it ends. Returns
    None if uiautomation is not installed.
    """
    global _uia_timeouts_set
    try:
        import uiautomation as auto
    except ImportError:
        return None
    if getattr(_uia_tls, "init", None) is None:
        _uia_tls.init = ComInit(auto)
    if not _uia_timeouts_set:
        _uia_timeouts_set = True
        set_uia_timeouts(auto)
    return auto


_CLSID_CUIAutomation8 = "{e22ad333-b25f-460c-83d0-0581107395c9}"


def set_uia_timeouts(auto) -> bool:
    """Cut UIA's per-call timeouts to 1 s for uiautomation's shared client.

    uiautomation creates the original CUIAutomation object, which has no
    IUIAutomation2 and so no timeouts. Swap in CUIAutomation8 (Windows 8+),
    which has them; the uiautomation API keeps working on it.
    """
    try:
        client = auto.uiautomation._AutomationClient.instance()
        core = client.UIAutomationCore
        try:
            ia2 = client.IUIAutomation.QueryInterface(core.IUIAutomation2)
        except Exception:
            import comtypes.client
            ia8 = comtypes.client.CreateObject(_CLSID_CUIAutomation8, interface=core.IUIAutomation)
            client.IUIAutomation = ia8
            client.ViewWalker = ia8.RawViewWalker
            ia2 = ia8.QueryInterface(core.IUIAutomation2)
        ia2.ConnectionTimeout = _UIA_TIMEOUT_MS
        ia2.TransactionTimeout = _UIA_TIMEOUT_MS
        return True
    except Exception as e:
        logger.debug(f"Could not set UIA timeouts: {e!r}")
        return False


# Browsers whose foreground window's page Document carries the page URL.
_BROWSER_EXES = {
    "chrome", "msedge", "firefox", "brave", "vivaldi", "opera", "chromium",
    "arc", "thorium", "librewolf", "waterfox", "floorp", "zen", "yandex",
}
# Only these count as the page URL. Others are browser pages (chrome://,
# about:), extension pages (moz-extension://) or helper frames.
_PAGE_URL_SCHEMES = ("http://", "https://", "file:///")
# Documents that sit next to the page, not instead of it.
_HELPER_DOC_SCHEMES = ("devtools://", "chrome-extension://", "moz-extension://",
                       "extension://", "edge-extension://")
# A web Document's value: one URL, nothing else.
_WEB_DOC_VALUE_RE = re.compile(r"(?:[a-z][a-z0-9+.\-]*://|about:)\S*\Z", re.IGNORECASE)


def pick_page_document(docs: list) -> Optional[Tuple[str, str]]:
    """Choose the visible page among a window's Documents.

    docs: dicts with name, url, nested (inside another Document: an iframe),
    onscreen (not IsOffscreen and a non-empty rect). Firefox also lists the
    Documents of background tabs, with their iframes, so only top-level,
    on-screen Documents count. Docked DevTools and extension side panels are
    Documents of their own and are dropped first. Exactly one must remain,
    and it must have a web or file URL; otherwise None. A browser page
    (about:, chrome://) still counts as the page, so it gives None rather
    than letting a side panel's URL stand in. A wrong URL is worse than none.
    """
    shown = [d for d in page_documents(docs) if d["url"]]
    if len(shown) != 1:
        return None
    page = shown[0]
    if not page["url"].lower().startswith(_PAGE_URL_SCHEMES):
        return None  # about:, chrome://, a PDF viewer... not a page URL
    return (page["name"] or "").strip(), page["url"]


def is_web_document(read_only, value) -> bool:
    """Web content (browsers, Electron apps) is a read-only Document whose
    value is its URL. Editable Documents are text areas (Notepad, Word), and
    a read-only text viewer has its text as the value."""
    return read_only is True and isinstance(value, str) and bool(_WEB_DOC_VALUE_RE.match(value))


def page_documents(docs: list) -> list:
    """The Documents a browser window shows as pages: top-level, on screen, not
    docked DevTools, extension panels or Chrome's own side panels
    (chrome://*.top-chrome/). Split view shows two."""
    return [d for d in docs if not d["nested"] and d["onscreen"]
            and not (d["url"] or "").lower().startswith(_HELPER_DOC_SCHEMES)
            and ".top-chrome" not in (d["url"] or "").lower()]


def _window_documents(hwnd: int) -> list:
    """All Document elements in a window, as dicts for pick_page_document()
    (element is the UIA element).

    Chrome and Firefox build this tree lazily: right after a window opens,
    the first query can find nothing.
    """
    auto = uia()
    if auto is None or not hwnd:
        return []
    ia = auto.uiautomation._AutomationClient.instance().IUIAutomation
    root = ia.ElementFromHandle(hwnd)
    cond = ia.CreatePropertyCondition(_UIA_ControlTypePropertyId, _UIA_DocumentControlTypeId)
    found = root.FindAll(_TreeScope_Descendants, cond)
    walker = ia.ControlViewWalker
    out = []
    for i in range(found.Length if found else 0):
        doc = found.GetElement(i)
        try:
            value = doc.GetCurrentPropertyValue(_UIA_ValueValuePropertyId)
            read_only = doc.GetCurrentPropertyValue(_UIA_ValueIsReadOnlyPropertyId)
            rect = doc.CurrentBoundingRectangle
            onscreen = not doc.CurrentIsOffscreen and rect.right > rect.left and rect.bottom > rect.top
            nested = False
            parent = walker.GetParentElement(doc)
            for _ in range(40):  # up to the window
                if not parent or ia.CompareElements(parent, root):
                    break
                if parent.CurrentControlType == _UIA_DocumentControlTypeId:
                    nested = True
                    break
                parent = walker.GetParentElement(parent)
            out.append({"name": doc.CurrentName, "url": value if isinstance(value, str) else "",
                        "nested": nested, "onscreen": onscreen, "element": doc,
                        "web": is_web_document(read_only, value)})
        except Exception as e:
            logger.debug(f"Skipped a Document: {e!r}")
            continue
    return out


# ── Accessibility text walk ──────────────────────────────────────────

# Subtrees with app chrome, not content (macOS skips its menu roles too).
_A11Y_SKIP_TYPES = {"MenuBarControl", "MenuControl", "MenuItemControl",
                    "TitleBarControl", "ScrollBarControl"}
# Outside web content, controls are window chrome: their labels are in the UI
# language and say nothing about what is shown ("Свернуть", "Extensions",
# other tabs' titles). Chosen by element type, so any UI language is covered.
# In a web page, buttons and tabs are page content and are kept.
_A11Y_NATIVE_CHROME_TYPES = {"ButtonControl", "SplitButtonControl", "ToolBarControl",
                             "StatusBarControl", "TabItemControl"}
# Web landmarks for site menus and sidebars. They repeat on every frame and
# push out the main content (macOS skips the same landmarks).
_A11Y_WEB_SKIP_ROLES = {"navigation", "complementary"}
# Name of the pane that hosts Chromium web content; a constant, never localized.
_CHROMIUM_HOST_PANE = "Chrome Legacy Window"
# Text areas give at most this much: their visible part, or the end of the value.
_A11Y_VISIBLE_ONLY_CHARS = 4000
# Generous: analysis trims its own prompt (see engine/analyzer.py).
_A11Y_MAX_TOTAL_CHARS = 300000
# Web content is fetched whole and walked locally, so these are cheap. Text
# sits up to ~40 levels below a Document (Electron apps).
_A11Y_WEB_MAX_NODES = 20000
_A11Y_WEB_MAX_DEPTH = 200


def _repeats(a: str, b: str) -> bool:
    """Whether the shorter text is the start or end of the longer one."""
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 4 and (long_.startswith(short) or long_.endswith(short))


def _add_line(text: str, texts: list, seen: set, budget: list) -> None:
    """Append a line unless it is a repeat; budget is [chars left]."""
    text = text.strip()
    if len(text) <= 1 or text in seen or budget[0] <= 0:
        return
    # A link's name often repeats in its own text child, or the other way
    # round with a prefix ("Idle Chat name", "Chat name"). Keep the longer.
    if texts and _repeats(text, texts[-1]):
        if len(text) > len(texts[-1]):
            budget[0] += len(texts.pop())
        else:
            return
    seen.add(text)
    text = text[:budget[0]]
    budget[0] -= len(text)
    texts.append(text)


def _cached_tree(element) -> Optional[dict]:
    """A web Document's subtree as plain dicts, fetched in one UIA call.

    Reading a page node by node costs a cross-process call per property:
    1.2 s for a 500-node Chrome page, against 0.04 s for one cached fetch.
    Nodes: type, name, value, role (ARIA), offscreen, password, children.
    """
    auto = uia()
    if auto is None or element is None:
        return None
    ia = auto.uiautomation._AutomationClient.instance().IUIAutomation
    request = ia.CreateCacheRequest()
    for pid in (_UIA_NamePropertyId, _UIA_ControlTypePropertyId, _UIA_IsPasswordPropertyId,
                _UIA_IsOffscreenPropertyId, _UIA_ValueValuePropertyId, _UIA_AriaRolePropertyId):
        request.AddProperty(pid)
    request.TreeScope = _TreeScope_Subtree
    root = element.BuildUpdatedCache(request)
    type_names = auto.ControlTypeNames
    left = [_A11Y_WEB_MAX_NODES]

    def _node(el, depth: int) -> dict:
        left[0] -= 1
        value = el.GetCachedPropertyValue(_UIA_ValueValuePropertyId)
        role = el.GetCachedPropertyValue(_UIA_AriaRolePropertyId)
        children = []
        kids = el.GetCachedChildren() if depth < _A11Y_WEB_MAX_DEPTH else None
        for i in range(kids.Length if kids else 0):
            if left[0] <= 0:
                break
            children.append(_node(kids.GetElement(i), depth + 1))
        return {"type": type_names.get(el.CachedControlType, ""), "name": el.CachedName or "",
                "value": value if isinstance(value, str) else "",
                "role": role.lower() if isinstance(role, str) else "",
                "offscreen": bool(el.CachedIsOffscreen), "password": bool(el.CachedIsPassword),
                "children": children}

    return _node(root, 0)


def web_text(node: dict, texts: list, seen: set, budget: list) -> None:
    """Visible text of a web Document (a browser page, an Electron app) from
    a _cached_tree() node.

    Reads only on-screen elements, skips site menus and sidebars (nav and
    complementary landmarks), menus, scroll bars and password fields.
    Document values are URLs and link values are targets: neither is text,
    and a raw URL would skip sanitize_url.
    """
    if (budget[0] <= 0 or node["password"] or node["type"] in _A11Y_SKIP_TYPES
            or node["role"] in _A11Y_WEB_SKIP_ROLES):
        return
    if not node["offscreen"]:
        name = node["name"]
        _add_line(name[:_A11Y_VISIBLE_ONLY_CHARS], texts, seen, budget)
        value = node["value"]
        if (value and node["type"] not in ("DocumentControl", "HyperlinkControl")
                and value.strip() != name.strip()):
            _add_line(value[-_A11Y_VISIBLE_ONLY_CHARS:], texts, seen, budget)
    for child in node["children"]:
        web_text(child, texts, seen, budget)
        if budget[0] <= 0:
            return


# ── Top window per display ───────────────────────────────────────────

_GWL_EXSTYLE = -20
_GW_OWNER = 4
_WS_EX_TOPMOST = 0x00000008
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_APPWINDOW = 0x00040000
_WS_EX_NOACTIVATE = 0x08000000
_DWMWA_CLOAKED = 14
# Shell windows that are never "the app on this display".
_SHELL_CLASSES = {
    "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW",
    "NotifyIconOverflowWindow", "TopLevelWindowForOverflowXamlIsland",
    "Windows.UI.Core.CoreWindow", "XamlExplorerHostIslandWindow",
}
_MIN_WINDOW_SIDE = 50


def is_app_window(visible: bool, minimized: bool, cloaked: bool, ex_style: int,
                  class_name: str, title: str, width: int, height: int) -> bool:
    """Whether a top-level window counts as the app shown on a display.

    Skips hidden, minimized and cloaked windows (other virtual desktops,
    suspended UWP apps), tool windows, click-through and always-on-top
    overlays (picture-in-picture, game overlays), the taskbar and desktop,
    untitled windows and tiny ones. macOS likewise counts only normal-layer
    windows.
    """
    if not visible or minimized or cloaked:
        return False
    if ex_style & _WS_EX_TOOLWINDOW and not ex_style & _WS_EX_APPWINDOW:
        return False
    if ex_style & (_WS_EX_TRANSPARENT | _WS_EX_NOACTIVATE | _WS_EX_TOPMOST):
        return False
    if class_name in _SHELL_CLASSES or not title.strip():
        return False
    return width >= _MIN_WINDOW_SIDE and height >= _MIN_WINDOW_SIDE


def center_in(rect: Tuple[int, int, int, int], x: int, y: int, width: int, height: int) -> bool:
    """Whether the center of rect (left, top, right, bottom) lies on the display."""
    left, top, right, bottom = rect
    cx, cy = (left + right) // 2, (top + bottom) // 2
    return x <= cx < x + width and y <= cy < y + height


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
        """Foreground window title.

        Electron apps often title every window with just the app name
        ("Slack"). Then the page title of the window's web Document is the
        real title, as on macOS.
        """
        try:
            _, user32 = _dlls()
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return None
            return self._best_title(hwnd, _window_text(hwnd), process_name(_window_pid(hwnd)) or "") or None
        except Exception:
            return None

    def _best_title(self, hwnd, title: str, app: str) -> str:
        """The window title, or the page title of that window's Document when
        the title is only the app name (Electron apps). Same rule as macOS,
        used for the focused window and for per-display labels."""
        if not title or title.strip().lower() == app.lower():
            page = self._document_title(hwnd, title)
            if page:
                return page
        return title

    def _document_title(self, hwnd, title: str) -> Optional[str]:
        """Name of the window's top on-screen Document, if it says more than the title."""
        try:
            docs = [d for d in _window_documents(hwnd) if not d["nested"] and d["onscreen"]]
        except Exception:
            return None
        for d in docs:
            name = (d["name"] or "").strip()
            if name and name.lower() != (title or "").strip().lower():
                return name
        return None

    def get_active_app_name(self) -> Optional[str]:
        """Get process name via Win32 APIs."""
        try:
            _, user32 = _dlls()
            hwnd = user32.GetForegroundWindow()
            return process_name(_window_pid(hwnd)) if hwnd else None
        except Exception:
            return None

    def get_front_window(self) -> Optional[dict]:
        """Foreground window as {"pid", "app_name", "title"}, all from one HWND."""
        try:
            _, user32 = _dlls()
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return None
            pid = _window_pid(hwnd)
            return {"pid": pid or None, "app_name": process_name(pid), "title": _window_text(hwnd) or None}
        except Exception:
            return None

    @property
    def can_find_top_window(self) -> bool:
        return True

    def _app_windows(self, x: Optional[int] = None, y: Optional[int] = None,
                     width: Optional[int] = None, height: Optional[int] = None,
                     first_only: bool = False) -> list:
        """App windows as (hwnd, title, rect), top of the z-order first.

        EnumWindows lists top-level windows from the top of the z-order down.
        With a display rect, only windows whose center is on it count.
        Coordinates are physical pixels, like mss monitors (the process is
        per-monitor DPI aware once mss has started).
        """
        import ctypes
        from ctypes import wintypes
        _, user32 = _dlls()
        found = []

        def _check(hwnd, _):
            try:
                rect = wintypes.RECT()
                if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    return True
                box = (rect.left, rect.top, rect.right, rect.bottom)
                if x is not None and not center_in(box, x, y, width, height):
                    return True
                cls = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, cls, 256)
                title = _window_text(hwnd)
                if not is_app_window(
                    visible=bool(user32.IsWindowVisible(hwnd)),
                    minimized=bool(user32.IsIconic(hwnd)),
                    cloaked=_is_cloaked(hwnd),
                    ex_style=user32.GetWindowLongW(hwnd, _GWL_EXSTYLE) & 0xFFFFFFFF,
                    class_name=cls.value, title=title,
                    width=rect.right - rect.left, height=rect.bottom - rect.top,
                ):
                    return True
                found.append((hwnd, title, box))
                return not first_only  # with first_only, the topmost match wins
            except Exception:
                return True

        try:
            user32.EnumWindows(_WNDENUMPROC(_check), 0)
        except Exception:
            return []
        return found

    def get_top_window_in(self, x: int, y: int, width: int, height: int) -> Optional[Tuple[str, Optional[str]]]:
        """(app, title) of the top app window whose center is on this display."""
        found = self._app_windows(x, y, width, height, first_only=True)
        if not found:
            return None
        hwnd, title, _ = found[0]
        app = process_name(_window_pid(hwnd))
        if not app:
            return None
        return app, self._best_title(hwnd, title, app) or app

    def list_visible_windows(self) -> list:
        """Every visible app window on all displays, top of the z-order first."""
        result = []
        for hwnd, title, (left, top, right, bottom) in self._app_windows():
            pid = _window_pid(hwnd)
            result.append({
                "owner": process_name(pid), "pid": pid, "title": title,
                "bounds": (left, top, right - left, bottom - top),
            })
        return result

    def get_browser_url(self) -> Optional[str]:
        """URL of the page in the foreground browser window, read from the page's
        UIA Document. Unlike the address bar it has the scheme and does not
        depend on the browser's UI language. None for non-browsers."""
        try:
            _, user32 = _dlls()
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return None
            if (process_name(_window_pid(hwnd)) or "").lower() not in _BROWSER_EXES:
                return None
            docs = _window_documents(hwnd)
            page = pick_page_document(docs)
            if page is None:
                # Schemes only: background tabs' URLs don't belong in logs.
                logger.debug("No single on-screen page Document: " + ", ".join(
                    f"{d['url'].split(':', 1)[0] or '-'}(nested={d['nested']}, onscreen={d['onscreen']})"
                    for d in docs))
            return page[1] if page else None
        except Exception as e:
            logger.debug(f"Browser URL lookup failed: {e!r}")
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
        """Extract text using the uiautomation library.

        Browsers give only their on-screen pages, never their own UI (tabs,
        toolbar, bookmarks, extension buttons): that is in the browser's UI
        language and says nothing about the page. No page Document found
        means no text, so analysis falls back to OCR.

        Other apps: the native walk (depth 8), then their web Documents
        (Electron apps), which sit 9+ levels down and are read whole.
        """
        auto = uia() or self._uia

        try:
            if not hwnd:
                hwnd = _dlls()[1].GetForegroundWindow()
            if not hwnd:
                return None

            texts, seen, budget = [], set(), [_A11Y_MAX_TOTAL_CHARS]
            docs = _window_documents(hwnd)
            if (process_name(_window_pid(hwnd)) or "").lower() in _BROWSER_EXES:
                pages = page_documents(docs)
            else:
                window = auto.ControlFromHandle(hwnd)
                if not window:
                    return None
                self._walk_tree(window, texts, depth=0, max_depth=8, seen=seen, budget=budget)
                pages = [d for d in page_documents(docs) if d["web"]]
            for page in pages:
                self._walk_web_document(page["element"], texts, seen, budget)

            return '\n'.join(texts) if texts else None

        except Exception:
            return None

    def _walk_web_document(self, element, texts: list, seen: set, budget: list) -> None:
        """Add a web Document's visible text (see web_text)."""
        try:
            tree = _cached_tree(element)
        except Exception as e:
            # A huge page can outlast the 1 s UIA timeout; OCR covers it.
            logger.debug(f"Could not read a web Document: {e!r}")
            return
        if tree:
            web_text(tree, texts, seen, budget)

    def _is_web_document(self, control) -> bool:
        auto = self._uia or uia()
        try:
            vp = control.GetPattern(auto.PatternId.ValuePattern)
            return vp is not None and is_web_document(vp.IsReadOnly, vp.Value)
        except Exception:
            return False

    def _walk_tree(self, control, texts: list, depth: int, max_depth: int = 8,
                   seen: Optional[set] = None, budget: Optional[list] = None):
        """Recursively walk the UI Automation tree and extract text.

        Same rules as the macOS walker: skips menus (and title bars and
        scroll bars), drops repeated lines, reads only the visible part of
        editable text areas, and stops after _A11Y_MAX_TOTAL_CHARS. Password
        fields are never read. Also skips window chrome by control type
        (buttons, toolbars, tabs) and web Documents, which
        _extract_uiautomation reads whole with web_text's rules.
        """
        seen = set() if seen is None else seen
        budget = [_A11Y_MAX_TOTAL_CHARS] if budget is None else budget
        if depth > max_depth or len(texts) > 500 or budget[0] <= 0:
            return

        try:
            control_type = control.ControlTypeName
            if control_type in _A11Y_SKIP_TYPES or control_type in _A11Y_NATIVE_CHROME_TYPES:
                return
            if control_type == "DocumentControl" and self._is_web_document(control):
                return  # read whole afterwards, see _extract_uiautomation

            name = control.Name
            if name and name.strip() and name != _CHROMIUM_HOST_PANE:
                # Some editors (Scintilla) put the whole document in the name.
                _add_line(name[:_A11Y_VISIBLE_ONLY_CHARS], texts, seen, budget)

            if not control.IsPassword and control_type != "HyperlinkControl":
                value = self._control_text(control, control_type)
                if value and value.strip() != (name or "").strip():
                    _add_line(value, texts, seen, budget)

            children = control.GetChildren()
            if children:
                for child in children:
                    self._walk_tree(child, texts, depth + 1, max_depth, seen, budget)
                    if len(texts) > 500 or budget[0] <= 0:
                        return

        except Exception:
            pass

    def _control_text(self, control, control_type: str) -> Optional[str]:
        """An element's value. Editable text areas (Notepad, editors) give only
        their visible lines, so a big file or a terminal's scrollback is not
        read whole. (Web Documents are read by web_text.)"""
        auto = self._uia or uia()
        try:
            vp = control.GetPattern(auto.PatternId.ValuePattern)
        except Exception:
            vp = None
        if control_type in ("DocumentControl", "EditControl"):
            read_only = None
            if vp is not None:
                try:
                    read_only = vp.IsReadOnly
                except Exception:
                    pass
            if not read_only:
                visible = self._visible_text(control)
                if visible is not None:
                    return visible
        if vp is None:
            return None
        try:
            value = vp.Value
        except Exception:
            return None
        if isinstance(value, str) and len(value) > _A11Y_VISIBLE_ONLY_CHARS:
            value = value[-_A11Y_VISIBLE_ONLY_CHARS:]
        return value

    def _visible_text(self, control) -> Optional[str]:
        """On-screen text of a text area via UIA TextPattern.GetVisibleRanges."""
        auto = self._uia or uia()
        try:
            tp = control.GetPattern(auto.PatternId.TextPattern)
            if tp is None:
                return None
            parts = [r.GetText(_A11Y_VISIBLE_ONLY_CHARS) for r in tp.GetVisibleRanges()]
            return "\n".join(p for p in parts if p)[:_A11Y_VISIBLE_ONLY_CHARS]
        except Exception:
            return None

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
