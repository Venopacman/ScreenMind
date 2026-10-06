"""
Windows UI event backend.

Input comes from low-level keyboard and mouse hooks (WH_KEYBOARD_LL,
WH_MOUSE_LL) on one thread that runs a GetMessageW loop. Element lookups
use UI Automation through the uiautomation package. No permissions needed.

Windows calls the hook callbacks on the hook thread. If a callback is slow,
Windows skips it, and after enough timeouts (LowLevelHooksTimeout) it removes
the hook without telling us. So the callbacks only build a RawEvent, put it
on the queue and return CallNextHookEx. A timer on the hook thread also
re-installs a hook that has gone silent while the user was active.

Element lookups, the clipboard and window polling run on the recorder's
enricher thread, never in a callback.
"""

import ctypes
import logging
import queue
import threading
import time
import unicodedata
from ctypes import wintypes
from typing import Callable, Optional

from screenmind.capture.ui_events.base import FrontWindow, PermissionStatus, UiEventBackend
from screenmind.capture.ui_events.models import (
    ElementInfo,
    KEY_ARROW,
    KEY_BACKSPACE,
    KEY_CHAR,
    KEY_ENTER,
    KEY_ESCAPE,
    KEY_OTHER,
    KEY_TAB,
    TEXT_INPUT_ROLES,
    RawEvent,
    normalize_uia_role,
)

logger = logging.getLogger("screenmind.capture.ui_events.windows")

# ── Win32 constants ──────────────────────────────────────────────────

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
WM_QUIT = 0x0012
WM_TIMER = 0x0113
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_LBUTTONDOWN = 0x0201
WM_RBUTTONDOWN = 0x0204
LLKHF_INJECTED = 0x10
LLMHF_INJECTED = 0x01
CF_UNICODETEXT = 13

VK_SHIFT, VK_CONTROL, VK_MENU, VK_CAPITAL = 0x10, 0x11, 0x12, 0x14
VK_LWIN, VK_RWIN = 0x5B, 0x5C

# ToUnicodeEx flag: do not change the keyboard state (Windows 10 1607+).
# Without it, our call would eat the dead key the user's app is waiting for.
TOUNICODE_NO_STATE_CHANGE = 0x4

# Normalized names for keys that are not text.
_VK_KEYS = {
    0x0D: KEY_ENTER,
    0x09: KEY_TAB,
    0x08: KEY_BACKSPACE,
    0x1B: KEY_ESCAPE,
    # arrows, home/end, page up/down, delete: cursor moves or text changes
    # we cannot follow
    0x25: KEY_ARROW, 0x26: KEY_ARROW, 0x27: KEY_ARROW, 0x28: KEY_ARROW,
    0x21: KEY_ARROW, 0x22: KEY_ARROW, 0x23: KEY_ARROW, 0x24: KEY_ARROW, 0x2E: KEY_ARROW,
}

# Modifier and lock keys on their own carry nothing; they are not queued.
_IGNORED_VKS = {
    VK_SHIFT, VK_CONTROL, VK_MENU, VK_CAPITAL, VK_LWIN, VK_RWIN,
    0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5,  # left/right shift, ctrl, alt
    0x90, 0x91,  # num lock, scroll lock
}

# Spacing accent that a dead key reports -> combining mark it adds.
_DEAD_TO_COMBINING = {
    "`": "̀", "´": "́", "'": "́", "^": "̂", "~": "̃",
    "¯": "̄", "˘": "̆", "˙": "̇", "¨": "̈", '"': "̈",
    "˚": "̊", "°": "̊", "˝": "̋", "ˇ": "̌", "¸": "̧", "˛": "̨",
}

# Clipboard formats that password managers and Windows use to mark secrets.
# https://learn.microsoft.com/windows/win32/dataxchg/clipboard-formats#cloud-clipboard-and-clipboard-history-formats
CLIP_EXCLUDE = "ExcludeClipboardContentFromMonitorProcessing"
CLIP_NO_HISTORY = "CanIncludeInClipboardHistory"
CLIP_NO_CLOUD = "CanUploadToCloudClipboard"

# A hook is re-installed if it saw nothing for this long while the user was
# active. False alarms (e.g. no typing while using the mouse) are harmless.
_HOOK_CHECK_MS = 10_000
_HOOK_SILENT_S = 30.0

# UIA timeouts (ms). The defaults let a hung app block a call for 20 s.
_UIA_TIMEOUT_MS = 1000

_MAX_VALUE_LEN = 200
_MAX_NAME_LEN = 100
_MAX_CONTAINER_NAME_LEN = 60

# When a click lands on a label or icon inside one of these, report the control.
_ACTIONABLE_ROLES = {
    "Button", "SplitButton", "Hyperlink", "MenuItem", "CheckBox", "RadioButton",
    "TabItem", "ListItem", "TreeItem", "ComboBox", "Edit", "DataItem", "HeaderItem",
}
_LABEL_ROLES = {"Text", "Image", "Custom", "Group", "Pane"}
# Layout containers; web views put long text in their names.
_CONTAINER_ROLES = {
    "Pane", "Group", "Window", "Custom", "List", "Tree", "Table", "DataGrid",
    "ToolBar", "TitleBar", "MenuBar", "Tab", "StatusBar", "Document", "ScrollBar",
}


# ── Pure helpers (unit-tested) ────────────────────────────────────────

def compose_dead_key(dead: str, base: str) -> str:
    """What Windows types for a dead key followed by `base`.

    Space gives the accent itself; a letter that has a precomposed form gets
    it ("´" + "e" -> "é"); anything else gives both characters.
    """
    if base == " ":
        return dead
    combining = _DEAD_TO_COMBINING.get(dead)
    if combining:
        composed = unicodedata.normalize("NFC", base + combining)
        if len(composed) == 1:
            return composed
    return dead + base


def is_shortcut(ctrl: bool, alt: bool, win: bool) -> bool:
    """Ctrl or Win make a shortcut, not text. Ctrl+Alt is AltGr on European
    layouts and types text (@, €, {). Alt alone drives menus."""
    if win:
        return True
    if ctrl and alt:
        return False
    return ctrl or alt


def shortcut_char(vk: int) -> Optional[str]:
    """Letter or digit of a shortcut by virtual key, so Ctrl+C is "c" on any layout."""
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk).lower()
    return None


class DeadKeyState:
    """Tracks a pending dead key between key presses (hook thread only).

    feed() takes ToUnicodeEx's return code and buffer: <0 is a dead key,
    0 is no character, >0 is the number of characters.
    """

    def __init__(self):
        self.pending: Optional[str] = None

    def reset(self):
        self.pending = None

    def feed(self, rc: int, chars: str):
        if rc < 0:
            self.pending = chars[:1] or None
            return KEY_OTHER, None
        if rc == 0:
            self.pending = None
            return KEY_OTHER, None
        text = chars[:rc]
        if self.pending:
            text = compose_dead_key(self.pending, text)
            self.pending = None
        if text.isprintable():
            return KEY_CHAR, text
        return KEY_OTHER, None


def clipboard_is_secret(has_format: Callable[[str], bool],
                        read_dword: Callable[[str], Optional[int]]) -> bool:
    """True if the clipboard owner asked monitors not to read it.

    Password managers (KeePass, 1Password, Bitwarden) set these formats.
    """
    if has_format(CLIP_EXCLUDE):
        return True
    for name in (CLIP_NO_HISTORY, CLIP_NO_CLOUD):
        if has_format(name) and read_dword(name) == 0:
            return True
    return False


# ── ctypes declarations ──────────────────────────────────────────────

LRESULT = wintypes.LPARAM
# WINFUNCTYPE exists only on Windows; the fallback keeps this module importable
# elsewhere, so the pure helpers above can be tested on any OS.
HOOKPROC = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def _load_user32():
    # Own WinDLL instance: argtypes here never clash with the keyboard package.
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
    u.SetWindowsHookExW.restype = wintypes.HHOOK
    u.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    u.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    u.CallNextHookEx.restype = LRESULT
    u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    u.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
                               wintypes.UINT, wintypes.UINT]
    u.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
    u.SetTimer.restype = ctypes.c_size_t
    u.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
    u.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
    u.GetAsyncKeyState.argtypes = [ctypes.c_int]
    u.GetAsyncKeyState.restype = ctypes.c_short
    u.GetKeyState.argtypes = [ctypes.c_int]
    u.GetKeyState.restype = ctypes.c_short
    u.GetForegroundWindow.restype = wintypes.HWND
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u.GetWindowThreadProcessId.restype = wintypes.DWORD
    u.GetKeyboardLayout.argtypes = [wintypes.DWORD]
    u.GetKeyboardLayout.restype = wintypes.HKL
    u.ToUnicodeEx.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_ubyte),
                              wintypes.LPWSTR, ctypes.c_int, wintypes.UINT, wintypes.HKL]
    u.GetClipboardSequenceNumber.restype = wintypes.DWORD
    u.OpenClipboard.argtypes = [wintypes.HWND]
    u.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    u.GetClipboardData.argtypes = [wintypes.UINT]
    u.GetClipboardData.restype = wintypes.HANDLE
    u.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    u.RegisterClipboardFormatW.restype = wintypes.UINT
    return u


def _load_kernel32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.GetCurrentThreadId.restype = wintypes.DWORD
    k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    k.GetModuleHandleW.restype = wintypes.HMODULE
    k.GetTickCount.restype = wintypes.DWORD
    k.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    k.GlobalSize.argtypes = [wintypes.HGLOBAL]
    k.GlobalSize.restype = ctypes.c_size_t
    return k


def _set_dpi_aware():
    """Per-monitor DPI awareness, so hook coordinates match UIA's (mss sets the
    same mode when it first captures). Fails harmlessly if already set."""
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except Exception:
        pass


class WindowsUiEventBackend(UiEventBackend):
    name = "windows"

    def __init__(self):
        # Cheap on purpose: create_backend() runs at every startup, even with
        # UI events off. Hooks, DLLs and UIA load in start() / on first use.
        self._u = None
        self._k = None
        self._thread: Optional[threading.Thread] = None
        self._thread_id = 0
        self._out: Optional[queue.SimpleQueue] = None
        self._hooks = {"keyboard": None, "mouse": None}
        self._procs = {}  # keep the ctypes callbacks alive
        self._last_seen = {"keyboard": 0.0, "mouse": 0.0}
        self.hook_reinstalls = 0
        self._dead = DeadKeyState()
        self._kbd_state = (ctypes.c_ubyte * 256)()
        self._char_buf = ctypes.create_unicode_buffer(8)
        self._auto = None
        self._uia_tls = threading.local()
        self._uia_timeouts_set = False
        self._clip_formats = {}

    def _dlls(self):
        if self._u is None:
            self._u = _load_user32()
            self._k = _load_kernel32()
        return self._u, self._k

    # ── Permissions ──────────────────────────────────────────────────

    def check_permissions(self) -> PermissionStatus:
        return PermissionStatus(input_monitoring=True, accessibility=True)

    def request_permissions(self) -> PermissionStatus:
        return self.check_permissions()

    # ── Hook thread ──────────────────────────────────────────────────

    def start(self, out: queue.SimpleQueue) -> bool:
        if self.is_running():
            return True
        self._dlls()
        _set_dpi_aware()
        self._out = out
        ready = threading.Event()
        result = {"ok": False}
        self._thread = threading.Thread(
            target=self._run_hooks, args=(ready, result), name="ui-events-hooks", daemon=True,
        )
        self._thread.start()
        ready.wait(timeout=3.0)
        return result["ok"]

    def stop(self) -> None:
        if self._thread is not None and self._thread_id:
            self._u.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            self._thread.join(timeout=2.0)
        self._thread = None
        self._thread_id = 0

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _install(self, kind: str) -> bool:
        u, k = self._u, self._k
        if kind not in self._procs:
            cb = self._keyboard_callback if kind == "keyboard" else self._mouse_callback
            self._procs[kind] = HOOKPROC(cb)
        hook_id = WH_KEYBOARD_LL if kind == "keyboard" else WH_MOUSE_LL
        new = u.SetWindowsHookExW(hook_id, self._procs[kind], k.GetModuleHandleW(None), 0)
        if not new:
            logger.warning(f"SetWindowsHookExW({kind}) failed: error {ctypes.get_last_error()}")
            return False
        # Install the new hook before removing the old one, so no event is lost.
        old, self._hooks[kind] = self._hooks[kind], new
        if old:
            u.UnhookWindowsHookEx(old)
        self._last_seen[kind] = time.monotonic()
        return True

    def _run_hooks(self, ready: threading.Event, result: dict):
        u, k = self._u, self._k
        msg = wintypes.MSG()
        # Create this thread's message queue before anyone posts WM_QUIT to it.
        u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        self._thread_id = k.GetCurrentThreadId()
        ok = self._install("keyboard") and self._install("mouse")
        if not ok:
            self._unhook_all()
            ready.set()
            return
        timer = u.SetTimer(None, 0, _HOOK_CHECK_MS, None)
        result["ok"] = True
        ready.set()
        logger.info("Input hooks installed")
        try:
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_TIMER:
                    self._check_hooks()
        finally:
            if timer:
                u.KillTimer(None, timer)
            self._unhook_all()
            logger.info("Input hooks removed")

    def _unhook_all(self):
        for kind, hook in self._hooks.items():
            if hook:
                self._u.UnhookWindowsHookEx(hook)
            self._hooks[kind] = None

    def _check_hooks(self):
        """Re-install a hook that Windows may have dropped (LowLevelHooksTimeout)."""
        info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
        if not self._u.GetLastInputInfo(ctypes.byref(info)):
            return
        idle_s = ((self._k.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0
        now = time.monotonic()
        for kind in ("keyboard", "mouse"):
            silent = now - self._last_seen[kind]
            if silent >= _HOOK_SILENT_S and idle_s < silent - 1.0:
                # Input happened after this hook last fired.
                logger.debug(f"{kind} hook silent for {silent:.0f}s while input arrived; re-installing")
                if self._install(kind):
                    self.hook_reinstalls += 1

    # Hook callbacks: no logging, no UIA, no I/O. Build a RawEvent and return.

    def _keyboard_callback(self, n_code, w_param, l_param):
        try:
            self._last_seen["keyboard"] = time.monotonic()
            if n_code >= 0 and w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                kb = KBDLLHOOKSTRUCT.from_address(l_param)
                if not kb.flags & LLKHF_INJECTED and kb.vkCode not in _IGNORED_VKS:
                    raw = self._key_event(kb.vkCode, kb.scanCode)
                    if raw is not None:
                        self._out.put(raw)
        except Exception:
            pass
        return self._u.CallNextHookEx(None, n_code, w_param, l_param)

    def _mouse_callback(self, n_code, w_param, l_param):
        try:
            self._last_seen["mouse"] = time.monotonic()
            if n_code >= 0 and w_param in (WM_LBUTTONDOWN, WM_RBUTTONDOWN):
                ms = MSLLHOOKSTRUCT.from_address(l_param)
                if not ms.flags & LLMHF_INJECTED:
                    self._out.put(RawEvent(
                        kind="mouse_down", ts=time.time(), x=ms.pt.x, y=ms.pt.y,
                        button="left" if w_param == WM_LBUTTONDOWN else "right",
                    ))
        except Exception:
            pass
        return self._u.CallNextHookEx(None, n_code, w_param, l_param)

    def _down(self, vk: int) -> bool:
        return bool(self._u.GetAsyncKeyState(vk) & 0x8000)

    def _key_event(self, vk: int, scan: int) -> Optional[RawEvent]:
        now = time.time()
        ctrl, alt = self._down(VK_CONTROL), self._down(VK_MENU)
        shortcut = is_shortcut(ctrl, alt, self._down(VK_LWIN) or self._down(VK_RWIN))
        key = _VK_KEYS.get(vk)
        if key is not None or shortcut:
            self._dead.reset()
            return RawEvent(kind="key", ts=now, key=key or KEY_OTHER, shortcut=shortcut,
                            shortcut_char=shortcut_char(vk) if shortcut else None)
        rc, chars = self._to_unicode(vk, scan, ctrl and alt)
        key, char = self._dead.feed(rc, chars)
        return RawEvent(kind="key", ts=now, key=key, char=char)

    def _to_unicode(self, vk: int, scan: int, altgr: bool):
        """Character(s) for a key in the foreground app's keyboard layout."""
        u = self._u
        state = self._kbd_state
        ctypes.memset(state, 0, 256)
        if self._down(VK_SHIFT):
            state[VK_SHIFT] = 0x80
        if altgr:
            state[VK_CONTROL] = state[VK_MENU] = 0x80
        state[VK_CAPITAL] = u.GetKeyState(VK_CAPITAL) & 1
        thread = u.GetWindowThreadProcessId(u.GetForegroundWindow(), None)
        hkl = u.GetKeyboardLayout(thread)
        buf = self._char_buf
        rc = u.ToUnicodeEx(vk, scan, state, buf, len(buf), TOUNICODE_NO_STATE_CHANGE, hkl)
        return rc, buf.value if rc else ""

    # ── UI Automation (enricher thread) ──────────────────────────────

    def _uia(self):
        """The uiautomation module, with COM set up for the calling thread."""
        if self._auto is None:
            import uiautomation as auto
            self._auto = auto
        if getattr(self._uia_tls, "init", None) is None:
            # Released when the thread ends (thread-local cleanup runs there).
            from screenmind.platform_support.windows import ComInit
            self._uia_tls.init = ComInit(self._auto)
        if not self._uia_timeouts_set:
            self._uia_timeouts_set = True
            self._set_uia_timeouts()
        return self._auto

    def _set_uia_timeouts(self):
        try:
            client = self._auto.uiautomation._AutomationClient.instance()
            ia2 = client.IUIAutomation.QueryInterface(client.UIAutomationCore.IUIAutomation2)
            ia2.ConnectionTimeout = _UIA_TIMEOUT_MS
            ia2.TransactionTimeout = _UIA_TIMEOUT_MS
        except Exception as e:
            logger.debug(f"Could not set UIA timeouts: {e}")

    def _value_pattern(self, control):
        try:
            return control.GetPattern(self._auto.PatternId.ValuePattern)
        except Exception:
            return None

    def _element_info(self, control) -> ElementInfo:
        role = normalize_uia_role(_safe(lambda: control.ControlTypeName))
        is_password = bool(_safe(lambda: control.IsPassword))
        name = _clean_str(_safe(lambda: control.Name))

        editable = None
        value = None
        vp = self._value_pattern(control) if role in TEXT_INPUT_ROLES or not is_password else None
        if role in TEXT_INPUT_ROLES:
            # A web page is a UIA Document too, but read-only; typing there is
            # shortcuts (j/k, space), not text.
            read_only = _safe(lambda: vp.IsReadOnly) if vp else None
            if read_only is not None:
                editable = not read_only
            elif role == "Document":
                editable = False
        elif vp is not None and not is_password and role not in _CONTAINER_ROLES:
            value = _clean_str(_safe(lambda: vp.Value))
            if value:
                value = value[:_MAX_VALUE_LEN]

        if name is None and role == "Text" and value:
            name = value
        if name is None and role in _ACTIONABLE_ROLES and role not in TEXT_INPUT_ROLES:
            name = self._child_label(control) or _clean_str(_safe(lambda: control.HelpText))
        if name and role in _CONTAINER_ROLES and len(name) > _MAX_CONTAINER_NAME_LEN:
            name = None
        if name and len(name) > _MAX_NAME_LEN:
            name = name[:_MAX_NAME_LEN - 3].rstrip() + "..."
        return ElementInfo(
            role=role,
            name=name,
            value=value,
            pid=_safe(lambda: int(control.ProcessId)) or None,
            is_password=is_password,
            editable=editable,
        )

    def _child_label(self, control) -> Optional[str]:
        """Icon buttons in web and Electron apps keep their label on a child."""
        try:
            child = control.GetFirstChildControl()
            for _ in range(4):
                if child is None:
                    break
                label = _clean_str(child.Name)
                if label:
                    return label
                child = child.GetNextSiblingControl()
        except Exception:
            pass
        return None

    def element_at(self, x: float, y: float) -> Optional[ElementInfo]:
        try:
            auto = self._uia()
            control = auto.ControlFromPoint(int(x), int(y))
        except Exception:
            return None
        if control is None:
            return None
        info = self._element_info(control)
        if info.role in _ACTIONABLE_ROLES or info.role not in _LABEL_ROLES:
            return info
        # Clicked a label or icon: walk up to the control it belongs to.
        parent = control
        for _ in range(3):
            parent = _safe(parent.GetParentControl)
            if parent is None:
                break
            role = normalize_uia_role(_safe(lambda: parent.ControlTypeName))
            if role in _ACTIONABLE_ROLES:
                p_info = self._element_info(parent)
                if not p_info.name:
                    p_info.name = info.name
                return p_info
        return info

    def focused_element(self) -> Optional[ElementInfo]:
        try:
            control = self._uia().GetFocusedControl()
        except Exception:
            return None
        return self._element_info(control) if control is not None else None

    # ── Windows and processes ────────────────────────────────────────

    def front_window(self) -> Optional[FrontWindow]:
        from screenmind.platform_support import adapter
        front = adapter().get_front_window()
        if not front:
            return None
        return FrontWindow(pid=front["pid"], app_name=front["app_name"], title=front["title"])

    def app_name_for_pid(self, pid: int) -> Optional[str]:
        from screenmind.platform_support.windows import process_name
        return process_name(pid)

    # ── Clipboard ────────────────────────────────────────────────────

    def clipboard_change_count(self) -> Optional[int]:
        try:
            u, _ = self._dlls()
            return int(u.GetClipboardSequenceNumber())
        except Exception:
            return None

    def _clip_format(self, name: str) -> int:
        if name not in self._clip_formats:
            self._clip_formats[name] = self._u.RegisterClipboardFormatW(name)
        return self._clip_formats[name]

    def _read_clip_dword(self, name: str) -> Optional[int]:
        h = self._u.GetClipboardData(self._clip_format(name))
        if not h:
            return None
        p = self._k.GlobalLock(h)
        if not p:
            return None
        try:
            if self._k.GlobalSize(h) < 4:
                return None
            return ctypes.c_uint32.from_address(p).value
        finally:
            self._k.GlobalUnlock(h)

    def read_clipboard(self) -> Optional[str]:
        try:
            u, k = self._dlls()
            opened = False
            for _ in range(3):  # another app may hold it for a moment
                if u.OpenClipboard(None):
                    opened = True
                    break
                time.sleep(0.02)
            if not opened:
                return None
            try:
                has = lambda name: bool(u.IsClipboardFormatAvailable(self._clip_format(name)))  # noqa: E731
                if clipboard_is_secret(has, self._read_clip_dword):
                    return None
                if not u.IsClipboardFormatAvailable(CF_UNICODETEXT):
                    return None
                h = u.GetClipboardData(CF_UNICODETEXT)
                if not h:
                    return None
                p = k.GlobalLock(h)
                if not p:
                    return None
                try:
                    return ctypes.wstring_at(p) or None
                finally:
                    k.GlobalUnlock(h)
            finally:
                u.CloseClipboard()
        except Exception:
            return None


def _safe(fn):
    """UIA properties raise COMError when an element goes away mid-call."""
    try:
        return fn()
    except Exception:
        return None


def _clean_str(v) -> Optional[str]:
    return v.strip() if isinstance(v, str) and v.strip() else None
