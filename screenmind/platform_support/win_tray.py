"""Windows tray icon through Shell_NotifyIconW (ctypes, no extra package).

One daemon thread owns a hidden window and its message loop. It sleeps in
GetMessageW and wakes for a click, a 2 s status timer, or a stop. Measured
cost: under 1 MB and one thread (pystray: about 3 MB and two more packages).
See docs/plans/packaging-spikes.md (Windows notes).

The menu and the status come from screenmind.tray.TrayController. Import this
module only on Windows.
"""

import ctypes
import logging
import threading
from ctypes import wintypes as W
from pathlib import Path
from typing import Optional

logger = logging.getLogger("screenmind.tray")

# Own WinDLL objects: argtypes set here never clash with other modules'
# settings on ctypes.windll.user32 (uiautomation, the UI-events hooks).
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)

WINDOW_CLASS = "ScreenMindTray"  # tests and checks find the window by this name
ICON_ID = 1
WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_CONTEXTMENU = 0x007B
WM_TIMER = 0x0113
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_TRAY = WM_APP + 1      # the shell's callback for clicks on the icon
WM_REFRESH = WM_APP + 2   # posted by refresh() from any thread
NIN_SELECT = 0x0400
NIN_KEYSELECT = 0x0401
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x10
SM_CXSMICON, SM_CYSMICON = 49, 50
MF_STRING, MF_GRAYED, MF_SEPARATOR = 0x0, 0x1, 0x800
TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD = 0x2, 0x80, 0x100
TIMER_ID = 1
# A left or right click (or the keyboard) opens the menu. There is no window to open.
_OPEN_MENU_ON = (WM_LBUTTONUP, WM_RBUTTONUP, WM_CONTEXTMENU, NIN_SELECT, NIN_KEYSELECT)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", W.DWORD), ("Data2", W.WORD), ("Data3", W.WORD), ("Data4", W.BYTE * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", W.DWORD), ("hWnd", W.HWND), ("uID", W.UINT), ("uFlags", W.UINT),
        ("uCallbackMessage", W.UINT), ("hIcon", W.HICON), ("szTip", W.WCHAR * 128),
        ("dwState", W.DWORD), ("dwStateMask", W.DWORD), ("szInfo", W.WCHAR * 256),
        ("uVersion", W.UINT), ("szInfoTitle", W.WCHAR * 64), ("dwInfoFlags", W.DWORD),
        ("guidItem", GUID), ("hBalloonIcon", W.HICON),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", W.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", W.HINSTANCE), ("hIcon", W.HICON),
        ("hCursor", W.HANDLE), ("hbrBackground", W.HBRUSH), ("lpszMenuName", W.LPCWSTR),
        ("lpszClassName", W.LPCWSTR),
    ]


def _proto(dll, name, restype, *argtypes):
    fn = getattr(dll, name)
    fn.restype = restype
    fn.argtypes = argtypes
    return fn


_GetModuleHandleW = _proto(_kernel32, "GetModuleHandleW", W.HMODULE, W.LPCWSTR)
_RegisterClassW = _proto(_user32, "RegisterClassW", W.ATOM, ctypes.POINTER(WNDCLASSW))
_UnregisterClassW = _proto(_user32, "UnregisterClassW", W.BOOL, W.LPCWSTR, W.HINSTANCE)
_CreateWindowExW = _proto(_user32, "CreateWindowExW", W.HWND, W.DWORD, W.LPCWSTR, W.LPCWSTR,
                          W.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                          W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID)
_DestroyWindow = _proto(_user32, "DestroyWindow", W.BOOL, W.HWND)
_DefWindowProcW = _proto(_user32, "DefWindowProcW", LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
_PostMessageW = _proto(_user32, "PostMessageW", W.BOOL, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
_PostQuitMessage = _proto(_user32, "PostQuitMessage", None, ctypes.c_int)
_GetMessageW = _proto(_user32, "GetMessageW", W.BOOL, ctypes.POINTER(W.MSG), W.HWND, W.UINT, W.UINT)
_TranslateMessage = _proto(_user32, "TranslateMessage", W.BOOL, ctypes.POINTER(W.MSG))
_DispatchMessageW = _proto(_user32, "DispatchMessageW", LRESULT, ctypes.POINTER(W.MSG))
_RegisterWindowMessageW = _proto(_user32, "RegisterWindowMessageW", W.UINT, W.LPCWSTR)
_SetTimer = _proto(_user32, "SetTimer", ctypes.c_size_t, W.HWND, ctypes.c_size_t, W.UINT, W.LPVOID)
_KillTimer = _proto(_user32, "KillTimer", W.BOOL, W.HWND, ctypes.c_size_t)
_LoadImageW = _proto(_user32, "LoadImageW", W.HANDLE, W.HINSTANCE, W.LPCWSTR, W.UINT,
                     ctypes.c_int, ctypes.c_int, W.UINT)
_DestroyIcon = _proto(_user32, "DestroyIcon", W.BOOL, W.HICON)
_GetSystemMetrics = _proto(_user32, "GetSystemMetrics", ctypes.c_int, ctypes.c_int)
_CreatePopupMenu = _proto(_user32, "CreatePopupMenu", W.HMENU)
_AppendMenuW = _proto(_user32, "AppendMenuW", W.BOOL, W.HMENU, W.UINT, ctypes.c_size_t, W.LPCWSTR)
_TrackPopupMenu = _proto(_user32, "TrackPopupMenu", W.BOOL, W.HMENU, W.UINT, ctypes.c_int,
                         ctypes.c_int, ctypes.c_int, W.HWND, W.LPVOID)
_DestroyMenu = _proto(_user32, "DestroyMenu", W.BOOL, W.HMENU)
_SetForegroundWindow = _proto(_user32, "SetForegroundWindow", W.BOOL, W.HWND)
_GetCursorPos = _proto(_user32, "GetCursorPos", W.BOOL, ctypes.POINTER(W.POINT))
_Shell_NotifyIconW = _proto(_shell32, "Shell_NotifyIconW", W.BOOL, W.DWORD,
                            ctypes.POINTER(NOTIFYICONDATAW))


class Win32Tray:
    """The tray icon. start() and stop() are called from main's thread."""

    def __init__(self, controller, icon_on: Path, icon_off: Path, poll_s: float = 2.0):
        self._controller = controller
        self._icon_paths = (str(icon_on), str(icon_off))
        self._poll_ms = max(2000, int(poll_s * 1000))  # never a busy loop
        self._hwnd: Optional[int] = None
        self._hinst = None
        self._icons = (None, None)  # (on, off) HICON
        self._wndproc = WNDPROC(self._on_message)  # keep a reference for the callback
        self._taskbar_created = 0
        self._added = False
        self._shown = None  # (tooltip, recording) last sent to the shell
        self._stopping = False
        self._ready = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── Called from other threads ────────────────────────────────────
    def start(self, timeout: float = 5.0) -> bool:
        """Start the thread. True once its window exists (the icon may still wait
        for Explorer at login; the status timer adds it when it can)."""
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self._hwnd is not None

    def refresh(self):
        """Update icon and tooltip now (thread-safe)."""
        if self._hwnd:
            _PostMessageW(self._hwnd, WM_REFRESH, 0, 0)

    def stop(self, timeout: float = 2.0):
        """Remove the icon at once, then end the thread. No ghost icon is left,
        even if the tray thread is busy."""
        self._stopping = True
        hwnd = self._hwnd
        if hwnd:
            self._delete_icon()
            _PostMessageW(hwnd, WM_CLOSE, 0, 0)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    # ── Tray thread ──────────────────────────────────────────────────
    def _run(self):
        try:
            self._create_window()
        except Exception:
            logger.exception("Tray icon: could not create its window")
            if self._hwnd:
                self._delete_icon()
                _DestroyWindow(self._hwnd)
                self._hwnd = None
            _UnregisterClassW(WINDOW_CLASS, self._hinst)
            self._ready.set()
            return
        self._ready.set()
        msg = W.MSG()
        while _GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _TranslateMessage(ctypes.byref(msg))
            _DispatchMessageW(ctypes.byref(msg))
        self._cleanup()

    def _create_window(self):
        self._hinst = _GetModuleHandleW(None)
        wc = WNDCLASSW(lpfnWndProc=self._wndproc, hInstance=self._hinst,
                       lpszClassName=WINDOW_CLASS)
        if not _RegisterClassW(ctypes.byref(wc)):
            raise ctypes.WinError(ctypes.get_last_error())
        # A hidden top-level window, not a message-only one: only top-level
        # windows get the TaskbarCreated broadcast after Explorer restarts.
        hwnd = _CreateWindowExW(0, WINDOW_CLASS, "ScreenMind", 0, 0, 0, 0, 0,
                                None, None, self._hinst, None)
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self._hwnd = hwnd
        self._taskbar_created = _RegisterWindowMessageW("TaskbarCreated")
        cx, cy = _GetSystemMetrics(SM_CXSMICON), _GetSystemMetrics(SM_CYSMICON)
        self._icons = tuple(
            _LoadImageW(None, path, IMAGE_ICON, cx, cy, LR_LOADFROMFILE) for path in self._icon_paths)
        if not self._icons[0]:
            logger.warning("Tray icon: could not load %s", self._icon_paths[0])
        if not _SetTimer(hwnd, TIMER_ID, self._poll_ms, None):
            logger.warning("Tray icon: no status timer; the status shows only after a click")
        self._update(force=True)

    def _cleanup(self):
        self._delete_icon()
        for icon in self._icons:
            if icon:
                _DestroyIcon(icon)
        self._icons = (None, None)
        self._hwnd = None
        _UnregisterClassW(WINDOW_CLASS, self._hinst)

    def _nid(self, flags: int) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = ICON_ID
        nid.uFlags = flags
        return nid

    def _update(self, force: bool = False):
        """Add the icon if it is missing, else change it when the status changed."""
        if self._stopping or not self._hwnd:
            return
        c = self._controller
        shown = (c.tooltip(), c.is_recording())
        if self._added and not force and shown == self._shown:
            return
        nid = self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._icons[0] if shown[1] or not self._icons[1] else self._icons[1]
        nid.szTip = shown[0]
        ok = False
        if self._added:
            ok = _Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
            if not ok:  # Explorer lost it: add it again
                self._added = False
        if not self._added:
            ok = _Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
            if ok:
                self._added = True
                logger.info("Tray icon shown (%s)", shown[0])
        if ok:
            self._shown = shown

    def _delete_icon(self):
        if self._hwnd and self._added:
            nid = self._nid(0)
            _Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            self._added = False

    def _show_menu(self):
        items = self._controller.menu()
        hmenu = _CreatePopupMenu()
        if not hmenu:
            return
        try:
            for i, item in enumerate(items, start=1):
                if item is None:
                    _AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
                else:
                    flags = MF_STRING | (0 if item.enabled and item.action else MF_GRAYED)
                    _AppendMenuW(hmenu, flags, i, item.label.replace("&", "&&"))
            pt = W.POINT()
            _GetCursorPos(ctypes.byref(pt))
            # Without this the menu does not close on a click elsewhere
            _SetForegroundWindow(self._hwnd)
            cmd = _TrackPopupMenu(hmenu, TPM_RIGHTBUTTON | TPM_NONOTIFY | TPM_RETURNCMD,
                                  pt.x, pt.y, 0, self._hwnd, None)
            _PostMessageW(self._hwnd, WM_NULL, 0, 0)
        finally:
            _DestroyMenu(hmenu)
        if 0 < cmd <= len(items) and items[cmd - 1] is not None:
            action = items[cmd - 1].action
            if action is not None:
                try:
                    action()
                except Exception:
                    logger.exception("Tray menu action failed: %s", items[cmd - 1].label)
        self._update()

    def _on_message(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                if (lparam & 0xFFFF) in _OPEN_MENU_ON:
                    self._show_menu()
                return 0
            if msg == WM_TIMER and wparam == TIMER_ID:
                self._update()
                return 0
            if msg == WM_REFRESH:
                self._update()
                return 0
            if msg == self._taskbar_created and self._taskbar_created:
                self._added = False  # Explorer restarted: the icon is gone
                self._update(force=True)
                return 0
            if msg == WM_CLOSE:
                self._stopping = True
                _KillTimer(hwnd, TIMER_ID)
                self._delete_icon()
                _DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                _PostQuitMessage(0)
                return 0
        except Exception:
            logger.exception("Tray icon: error handling message 0x%x", msg)
        return _DefWindowProcW(hwnd, msg, wparam, lparam)
