"""Tests for the Windows UI event backend: the parts that need no OS.

Key translation, role normalization, clipboard secret checks and element
parsing run against fakes. The live hooks are checked by hand with
`python -m screenmind.capture.ui_events` (see docs/plans/ui-events.md).
"""

import sys
import types
from unittest.mock import MagicMock

import pytest

from screenmind.capture.ui_events.models import (
    KEY_ARROW,
    KEY_BACKSPACE,
    KEY_CHAR,
    KEY_ENTER,
    KEY_ESCAPE,
    KEY_OTHER,
    KEY_TAB,
    ElementInfo,
    EventType,
    RawEvent,
    normalize_uia_role,
    role_label,
)
from screenmind.capture.ui_events.recorder import PASSWORD_PLACEHOLDER, UiEventRecorder

win = pytest.importorskip("screenmind.capture.ui_events.windows")


# ── Keys ────────────────────────────────────────────────────────────


class TestKeys:
    @pytest.mark.parametrize("vk, key", [
        (0x0D, KEY_ENTER), (0x09, KEY_TAB), (0x08, KEY_BACKSPACE), (0x1B, KEY_ESCAPE),
        (0x25, KEY_ARROW), (0x28, KEY_ARROW), (0x24, KEY_ARROW), (0x2E, KEY_ARROW),
    ])
    def test_control_keys(self, vk, key):
        assert win._VK_KEYS[vk] == key

    def test_letters_are_not_control_keys(self):
        assert 0x41 not in win._VK_KEYS  # "A" goes through ToUnicodeEx

    def test_modifiers_alone_are_ignored(self):
        for vk in (0x10, 0x11, 0x12, 0xA0, 0xA5, 0x5B, 0x14):
            assert vk in win._IGNORED_VKS

    @pytest.mark.parametrize("ctrl, alt, winkey, expected", [
        (True, False, False, True),    # Ctrl+C
        (False, False, True, True),    # Win+E
        (True, True, False, False),    # AltGr+Q = "@" on a German layout
        (False, True, False, True),    # Alt+F opens a menu, not text
        (False, False, False, False),
    ])
    def test_is_shortcut(self, ctrl, alt, winkey, expected):
        assert win.is_shortcut(ctrl, alt, winkey) is expected

    def test_shortcut_char_is_layout_independent(self):
        assert win.shortcut_char(0x43) == "c"
        assert win.shortcut_char(0x58) == "x"
        assert win.shortcut_char(0x31) == "1"
        assert win.shortcut_char(0x70) is None  # F1


class TestDeadKeys:
    @pytest.mark.parametrize("dead, base, out", [
        ("´", "e", "é"),
        ("^", "a", "â"),
        ("¨", "u", "ü"),
        ('"', "o", "ö"),   # US-International
        ("`", "A", "À"),
        ("ˇ", "c", "č"),
        ("´", " ", "´"),   # dead key + space types the accent
        ("´", "x", "´x"),  # no precomposed form: both characters
    ])
    def test_compose(self, dead, base, out):
        assert win.compose_dead_key(dead, base) == out

    def test_dead_key_then_letter(self):
        s = win.DeadKeyState()
        assert s.feed(-1, "´") == (KEY_OTHER, None)
        assert s.feed(1, "e") == (KEY_CHAR, "é")
        assert s.feed(1, "e") == (KEY_CHAR, "e")  # pending was consumed

    def test_plain_char(self):
        assert win.DeadKeyState().feed(1, "q") == (KEY_CHAR, "q")

    def test_no_char_resets_pending(self):
        s = win.DeadKeyState()
        s.feed(-1, "^")
        assert s.feed(0, "") == (KEY_OTHER, None)
        assert s.feed(1, "a") == (KEY_CHAR, "a")

    def test_control_char_is_not_text(self):
        assert win.DeadKeyState().feed(1, "\x7f") == (KEY_OTHER, None)

    def test_only_rc_chars_are_used(self):
        assert win.DeadKeyState().feed(1, "ab") == (KEY_CHAR, "a")


# ── Roles ───────────────────────────────────────────────────────────


class TestRoles:
    @pytest.mark.parametrize("name, role", [
        ("EditControl", "Edit"),
        ("DocumentControl", "Document"),
        ("ButtonControl", "Button"),
        ("ListItemControl", "ListItem"),
        ("Control", "Control"),
        (None, None),
        ("", None),
    ])
    def test_normalize(self, name, role):
        assert normalize_uia_role(name) == role

    @pytest.mark.parametrize("role, label", [
        ("Edit", "text field"),
        ("Document", "document"),
        ("Button", "button"),
        ("Hyperlink", "link"),
        ("ListItem", "list item"),
        ("MenuItem", "menu item"),
        ("TabItem", "tab"),
        ("AXButton", "button"),       # macOS unchanged
        ("AXTextField", "text field"),
        ("AXDisclosureTriangle", "toggle"),
    ])
    def test_labels(self, role, label):
        assert role_label(role) == label

    def test_uia_text_inputs(self):
        assert ElementInfo(role="Edit").is_text_input
        assert ElementInfo(role="Document", editable=True).is_text_input
        assert not ElementInfo(role="Document", editable=False).is_text_input
        assert not ElementInfo(role="Edit", editable=False).is_text_input
        assert not ElementInfo(role="Button").is_text_input
        assert ElementInfo(role="AXTextField").is_text_input  # editable unknown on macOS


# ── Clipboard ───────────────────────────────────────────────────────


class TestClipboardSecret:
    def check(self, formats: dict):
        """formats: name -> DWORD value (None = present without a value)."""
        return win.clipboard_is_secret(lambda n: n in formats, lambda n: formats.get(n))

    def test_plain_text(self):
        assert self.check({}) is False

    def test_exclude_from_monitoring(self):
        assert self.check({win.CLIP_EXCLUDE: None}) is True

    def test_no_history(self):
        assert self.check({win.CLIP_NO_HISTORY: 0}) is True

    def test_no_cloud(self):
        assert self.check({win.CLIP_NO_CLOUD: 0}) is True

    def test_allowed_values_are_not_secret(self):
        assert self.check({win.CLIP_NO_HISTORY: 1, win.CLIP_NO_CLOUD: 1}) is False

    def test_unreadable_value_is_not_secret(self):
        assert self.check({win.CLIP_NO_HISTORY: None}) is False


# ── Element parsing with fake UIA controls ──────────────────────────


class FakeValuePattern:
    def __init__(self, value="", read_only=False):
        self.Value = value
        self.IsReadOnly = read_only


class FakeControl:
    def __init__(self, type_name, name="", password=False, vp=None, pid=42,
                 children=(), parent=None, help_text=""):
        self.ControlTypeName = type_name
        self.Name = name
        self.IsPassword = password
        self.ProcessId = pid
        self.HelpText = help_text
        self._vp = vp
        self._children = list(children)
        self._parent = parent
        for i, c in enumerate(self._children):
            c._parent = self
            c._next = self._children[i + 1] if i + 1 < len(self._children) else None

    def GetPattern(self, pattern_id):
        return self._vp

    def GetParentControl(self):
        return self._parent

    def GetFirstChildControl(self):
        return self._children[0] if self._children else None

    def GetNextSiblingControl(self):
        return getattr(self, "_next", None)


@pytest.fixture
def backend(monkeypatch):
    b = win.WindowsUiEventBackend()
    fake_auto = types.SimpleNamespace(PatternId=types.SimpleNamespace(ValuePattern=10002))
    b._auto = fake_auto
    return b


class TestElementInfo:
    def test_password_field(self, backend):
        info = backend._element_info(FakeControl(
            "EditControl", "Password", password=True, vp=FakeValuePattern("hunter2")))
        assert (info.role, info.name, info.is_password, info.value) == ("Edit", "Password", True, None)
        assert info.is_text_input

    def test_text_field_value_is_not_read(self, backend):
        info = backend._element_info(FakeControl(
            "EditControl", "Message", vp=FakeValuePattern("my draft message")))
        assert info.value is None
        assert info.editable is True

    def test_read_only_document_is_not_text_input(self, backend):
        info = backend._element_info(FakeControl(
            "DocumentControl", "Inbox", vp=FakeValuePattern("https://mail", read_only=True)))
        assert info.editable is False
        assert not info.is_text_input

    def test_document_without_value_pattern_is_not_text_input(self, backend):
        info = backend._element_info(FakeControl("DocumentControl", "Page"))
        assert not info.is_text_input

    def test_editable_document(self, backend):
        info = backend._element_info(FakeControl("DocumentControl", "Text editor", vp=FakeValuePattern()))
        assert info.is_text_input

    def test_value_cut_to_200_chars(self, backend):
        info = backend._element_info(FakeControl("ComboBoxControl", "Font", vp=FakeValuePattern("x" * 500)))
        assert len(info.value) == 200

    def test_long_container_name_dropped(self, backend):
        info = backend._element_info(FakeControl("PaneControl", "word " * 30))
        assert info.name is None

    def test_long_name_cut(self, backend):
        info = backend._element_info(FakeControl("ButtonControl", "a" * 150))
        assert len(info.name) == 100 and info.name.endswith("...")

    def test_icon_button_takes_child_label(self, backend):
        btn = FakeControl("ButtonControl", "", children=[FakeControl("ImageControl", ""),
                                                         FakeControl("TextControl", "Send")])
        assert backend._element_info(btn).name == "Send"

    def test_button_falls_back_to_help_text(self, backend):
        assert backend._element_info(FakeControl("ButtonControl", "", help_text="Settings")).name == "Settings"

    def test_pid(self, backend):
        assert backend._element_info(FakeControl("ButtonControl", "OK", pid=7)).pid == 7

    def test_click_on_label_reports_parent_control(self, backend):
        label = FakeControl("TextControl", "Send")
        FakeControl("ButtonControl", "", children=[label])
        backend._auto.ControlFromPoint = lambda x, y: label
        info = backend.element_at(5, 5)
        assert (info.role, info.name) == ("Button", "Send")

    def test_click_on_plain_text_stays_text(self, backend):
        label = FakeControl("TextControl", "Hello")
        FakeControl("PaneControl", "", children=[label])
        backend._auto.ControlFromPoint = lambda x, y: label
        assert backend.element_at(5, 5).role == "Text"

    def test_uia_errors_return_none(self, backend):
        def boom(x, y):
            raise OSError("COM error")
        backend._auto.ControlFromPoint = boom
        assert backend.element_at(1, 1) is None

    def test_scintilla_editor_is_a_text_input(self, backend):
        editor = FakeControl("PaneControl", "Hi, my whole document text is the name")
        editor.ClassName = "Scintilla"
        info = backend._element_info(editor)
        assert (info.role, info.editable, info.is_text_input) == ("Edit", True, True)
        assert info.name is None and info.value is None  # the name is the document

    def test_other_panes_are_not_text_inputs(self, backend):
        pane = FakeControl("PaneControl", "Toolbar")
        pane.ClassName = "ReBarWindow32"
        assert not backend._element_info(pane).is_text_input

    def test_no_permissions_needed(self, backend):
        assert backend.check_permissions().all_granted
        assert backend.request_permissions().all_granted


# ── Recorder with Windows-style elements ────────────────────────────


class WinFake:
    """Minimal backend returning Windows-style ElementInfo."""
    name = "fake-windows"

    def __init__(self, focus):
        self.focus = focus

    def check_permissions(self):
        return win.PermissionStatus(True, True)

    def start(self, out):
        return True

    def stop(self):
        pass

    def element_at(self, x, y):
        return ElementInfo(role="Button", name="Send", pid=1)

    def focused_element(self):
        return self.focus

    def front_window(self):
        return win.FrontWindow(pid=1, app_name="notepad", title="notes.txt - Notepad")

    def app_name_for_pid(self, pid):
        return "notepad"

    def clipboard_change_count(self):
        return 0

    def read_clipboard(self):
        return None

    def tap_stats(self):
        return {}


@pytest.fixture
def ui_settings(monkeypatch):
    from screenmind.config import settings
    monkeypatch.setattr(settings, "ui_events_enabled", True)
    monkeypatch.setattr(settings, "ui_events_types", "click,app_switch,text,clipboard")
    monkeypatch.setattr(settings, "event_triggered_capture", False)
    monkeypatch.setattr(settings, "blocked_apps", "")
    monkeypatch.setattr(settings, "sensitive_filter_enabled", True)
    return settings


def _type(r, text, start=101.0):
    for i, c in enumerate(text):
        t = start + i * 0.05
        r._tick(RawEvent(kind="key", ts=t, key=KEY_CHAR, char=c), t)


def _stored(db):
    return [e for call in db.insert_ui_events.call_args_list for e in call.args[0]]


def _recorder(focus):
    db = MagicMock()
    r = UiEventRecorder(database=db, capture_worker=None, backend=WinFake(focus))
    r._clip_count = 0
    r._tick(None, 100.0)
    return r, db


class TestRecorderWithUiaRoles:
    def test_typing_in_edit_is_recorded(self, ui_settings):
        r, db = _recorder(ElementInfo(role="Edit", name="Search", pid=1, editable=True))
        _type(r, "hello")
        r.flush_for_capture()
        [e] = _stored(db)
        assert (e.type, e.text, e.element_role, e.app_name) == (EventType.TEXT, "hello", "Edit", "notepad")
        assert "typed \"hello\" in text field" in e.describe()

    def test_typing_in_read_only_page_is_dropped(self, ui_settings):
        r, db = _recorder(ElementInfo(role="Document", name="Inbox", pid=1, editable=False))
        _type(r, "jjk")
        r.flush_for_capture()
        assert _stored(db) == []

    def test_password_edit_stores_placeholder_only(self, ui_settings):
        r, db = _recorder(ElementInfo(role="Edit", name="Password", pid=1, is_password=True, editable=True))
        _type(r, "hunter2")
        r.flush_for_capture()
        [e] = _stored(db)
        assert e.text == PASSWORD_PLACEHOLDER
        assert "hunter2" not in repr(e)

    def test_blocked_app_by_exe_name(self, ui_settings, monkeypatch):
        monkeypatch.setattr(ui_settings, "blocked_apps", "Notepad")
        r, db = _recorder(ElementInfo(role="Edit", name="Search", pid=1, editable=True))
        _type(r, "secret")
        r._tick(RawEvent(kind="mouse_down", ts=105.0, x=1, y=1), 105.0)
        r.flush_for_capture()
        assert _stored(db) == []


# ── Wiring ──────────────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_create_backend_on_windows():
    pytest.importorskip("uiautomation")
    from screenmind.capture.ui_events.recorder import create_backend
    assert isinstance(create_backend(), win.WindowsUiEventBackend)


def test_create_backend_without_uiautomation(monkeypatch):
    import importlib.util
    from screenmind.capture.ui_events import recorder
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert recorder.create_backend() is None
