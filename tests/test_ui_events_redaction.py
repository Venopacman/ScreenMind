"""Sensitive-data filter on recorded UI events, before they reach the DB.
All values here are made up."""

from screenmind.capture.ui_events.base import FrontWindow
from screenmind.capture.ui_events.models import ElementInfo, EventType, KEY_ENTER, RawEvent
from screenmind.config import settings

from test_ui_events import OTHER_FIELD, ctrl, rec, stored, type_text, ui_settings  # noqa: F401

PW = "[REDACTED:password]"
VALUE = "abc123def456"


def test_typed_dash_password_is_redacted(rec):
    r, b, db, cw = rec
    type_text(r, f"pwd - {VALUE}", 101.0)
    r.flush_for_capture()
    [e] = stored(db)
    assert e.text == f"pwd - {PW}"
    assert VALUE not in repr(e)


def test_value_after_enter_is_redacted(rec):
    r, b, db, cw = rec
    type_text(r, "pwd -", 101.0)
    r._tick(ctrl(KEY_ENTER, 101.5), 101.5)
    type_text(r, VALUE, 102.0)
    r.flush_for_capture()
    assert [e.text for e in stored(db)] == ["pwd -", PW]


def test_value_after_typing_pause_is_redacted(rec):
    r, b, db, cw = rec
    type_text(r, "password:", 101.0)
    r._tick(None, 104.0)  # idle flush ends the first chunk
    type_text(r, "s3cr3tValue", 105.0)
    r.flush_for_capture()
    assert [e.text for e in stored(db)] == ["password:", PW]


def test_label_does_not_carry_to_another_field(rec):
    r, b, db, cw = rec
    type_text(r, "pwd -", 101.0)
    r._tick(ctrl(KEY_ENTER, 101.5), 101.5)
    b.focus = OTHER_FIELD
    type_text(r, VALUE, 102.0)
    r.flush_for_capture()
    assert [e.text for e in stored(db)] == ["pwd -", VALUE]


def test_label_carry_expires(rec):
    r, b, db, cw = rec
    type_text(r, "pwd -", 101.0)
    r._tick(ctrl(KEY_ENTER, 101.5), 101.5)
    type_text(r, VALUE, 200.0)
    r.flush_for_capture()
    assert stored(db)[-1].text == VALUE


def test_prose_after_label_is_kept(rec):
    r, b, db, cw = rec
    type_text(r, "I forgot my password", 101.0)
    r._tick(ctrl(KEY_ENTER, 102.5), 102.5)
    type_text(r, "thanks for the reset", 103.0)
    r.flush_for_capture()
    assert [e.text for e in stored(db)] == ["I forgot my password", "thanks for the reset"]


def test_clipboard_is_redacted(rec):
    r, b, db, cw = rec
    b.clip_count = 1
    b.clip_text = f"root pwd - {VALUE}\nhost db1"
    r._tick(None, 102.0)
    r.flush_for_capture()
    [e] = stored(db)
    assert e.type == EventType.CLIPBOARD
    assert e.text == f"root pwd - {PW}\nhost db1"


def test_clicked_element_value_and_name_are_redacted(rec):
    r, b, db, cw = rec
    b.element = ElementInfo(role="AXStaticText", name=f"token: {VALUE}",
                            value=f"pwd - {VALUE}", pid=1)
    r._tick(RawEvent(kind="mouse_down", ts=101.0, x=1, y=1), 101.0)
    r.flush_for_capture()
    [e] = stored(db)
    assert e.element_name == f"token: {PW}"
    assert e.element_value == f"pwd - {PW}"


def test_window_title_is_redacted(rec):
    r, b, db, cw = rec
    b.front = FrontWindow(pid=2, app_name="Notepad++", title=f"pwd - {VALUE}.txt - Notepad++")
    r._tick(None, 101.0)
    r.flush_for_capture()
    [e] = stored(db)
    assert e.type == EventType.APP_SWITCH
    assert VALUE not in e.window_title


def test_filter_off_stores_text_as_typed(rec, monkeypatch):
    r, b, db, cw = rec
    monkeypatch.setattr(settings, "sensitive_filter_enabled", False)
    type_text(r, f"pwd - {VALUE}", 101.0)
    r.flush_for_capture()
    assert stored(db)[0].text == f"pwd - {VALUE}"
