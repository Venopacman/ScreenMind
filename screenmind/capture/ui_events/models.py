"""
Data types shared by the UI event backends, the text buffer and the recorder.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class EventType(str, Enum):
    """Kinds of UI events we store."""

    CLICK = "click"
    TEXT = "text"
    APP_SWITCH = "app_switch"
    WINDOW_FOCUS = "window_focus"
    CLIPBOARD = "clipboard"


ALL_EVENT_TYPES = [t.value for t in EventType]

# Normalized key names produced by the backends. "char" means a printable
# character is in RawEvent.char; everything else is a control key.
KEY_CHAR = "char"
KEY_ENTER = "enter"
KEY_TAB = "tab"
KEY_BACKSPACE = "backspace"
KEY_ARROW = "arrow"
KEY_ESCAPE = "escape"
KEY_OTHER = "other"


@dataclass
class RawEvent:
    """An input event as the OS hook saw it. Built inside the hook callback,
    so it must stay cheap: no accessibility calls, no I/O."""

    kind: str  # "mouse_down" | "key"
    ts: float  # time.time()
    x: Optional[float] = None
    y: Optional[float] = None
    button: Optional[str] = None  # "left" | "right"
    key: Optional[str] = None  # one of the KEY_* names
    char: Optional[str] = None
    shortcut: bool = False  # Cmd/Ctrl held: a shortcut, not text input
    shortcut_char: Optional[str] = None  # e.g. "c" for Cmd+C


@dataclass
class ElementInfo:
    """Accessibility info about a UI element."""

    role: Optional[str] = None
    subrole: Optional[str] = None
    name: Optional[str] = None
    value: Optional[str] = None
    pid: Optional[int] = None
    is_password: bool = False

    @property
    def is_text_input(self) -> bool:
        return (self.role or "") in TEXT_INPUT_ROLES

    def identity(self) -> tuple:
        """Stable identity for "is this still the same field?" checks.
        The value changes while typing, so it is left out."""
        return (self.pid, self.role, self.subrole, self.name)


TEXT_INPUT_ROLES = {
    "AXTextField", "AXTextArea", "AXComboBox", "AXSearchField",
    "Edit", "Document",  # UIA control types
}


@dataclass
class UiEvent:
    """A processed event, ready to store in the ui_events table."""

    timestamp: datetime
    type: EventType
    app_name: Optional[str] = None
    window_title: Optional[str] = None
    element_role: Optional[str] = None
    element_name: Optional[str] = None
    element_value: Optional[str] = None
    text: Optional[str] = None
    x: Optional[int] = None
    y: Optional[int] = None
    extra: dict = field(default_factory=dict)

    def describe(self) -> str:
        """One short human-readable line, used in the Gemma prompt and the UI."""
        return describe_event(
            self.type.value, self.app_name, self.window_title,
            self.element_role, self.element_name, self.text,
        )


_ROLE_LABELS = {
    "AXButton": "button", "AXMenuItem": "menu item", "AXMenuBarItem": "menu",
    "AXLink": "link", "AXCheckBox": "checkbox", "AXRadioButton": "option",
    "AXTab": "tab", "AXRadioGroup": "tab", "AXPopUpButton": "dropdown",
    "AXTextField": "text field", "AXTextArea": "text area",
    "AXSearchField": "search field", "AXComboBox": "combo box",
    "AXStaticText": "text", "AXImage": "image", "AXCell": "cell", "AXRow": "row",
    "AXDisclosureTriangle": "toggle", "AXWebArea": "page",
}


def role_label(role: Optional[str]) -> str:
    if not role:
        return "element"
    return _ROLE_LABELS.get(role, role[2:].lower() if role.startswith("AX") else role.lower())


def describe_event(
    type_: str,
    app_name: Optional[str],
    window_title: Optional[str],
    element_role: Optional[str],
    element_name: Optional[str],
    text: Optional[str],
) -> str:
    app = app_name or "unknown app"
    if type_ == EventType.APP_SWITCH.value:
        return f"switched to {app}" + (f" ({window_title})" if window_title and window_title != app else "")
    if type_ == EventType.WINDOW_FOCUS.value:
        return f"opened window \"{window_title}\" in {app}"
    if type_ == EventType.CLICK.value:
        label = role_label(element_role)
        target = f'{label} "{element_name}"' if element_name else label
        return f"clicked {target} in {app}"
    if type_ == EventType.TEXT.value:
        where = role_label(element_role)
        if element_name:
            where = f'{where} "{element_name}"'
        return f'typed "{text}" in {where} ({app})'
    if type_ == EventType.CLIPBOARD.value:
        return f'copied "{text}" in {app}'
    return f"{type_} in {app}"


def format_user_actions(rows, max_lines: int = 10, max_chars: int = 1200) -> Optional[str]:
    """Turn ui_events rows (dicts, oldest first) into a short bullet list.

    Keeps the most recent lines, drops repeats, and caps the size so the
    block stays around 300 tokens in the Gemma prompt.
    """
    lines = []
    for r in rows:
        if r.get("type") == EventType.CLICK.value and not r.get("element_name"):
            # "clicked group in Slack" says nothing the screenshot doesn't.
            continue
        text = r.get("text")
        if text and len(text) > 160:
            text = text[:157] + "..."
        line = describe_event(
            r.get("type"), r.get("app_name"), r.get("window_title"),
            r.get("element_role"), r.get("element_name"), text,
        )
        if lines and lines[-1] == line:
            continue
        lines.append(line)
    if not lines:
        return None
    lines = lines[-max_lines:]
    out = "\n".join(f"- {line}" for line in lines)
    while len(out) > max_chars and len(lines) > 1:
        lines = lines[1:]
        out = "\n".join(f"- {line}" for line in lines)
    return out[:max_chars]
