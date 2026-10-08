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


# ── Contact details never reach the DB ───────────────────────────────

EMAIL = "jane.doe@example.com"
PHONE = "+1 (415) 555-0132"
IBAN = "DE89 3704 0044 0532 0130 00"


def test_contact_details_never_reach_the_db(ui_settings, monkeypatch, db):
    """With the default filter types, emails, phones and IBANs typed, copied
    or shown in a title are stored redacted. Checked on a real SQLite file."""
    import sqlite3
    from unittest.mock import MagicMock

    from screenmind.capture.ui_events.recorder import UiEventRecorder
    from screenmind.config import Settings
    from test_ui_events import FakeBackend

    monkeypatch.setattr(settings, "sensitive_filter_types",
                        Settings.model_fields["sensitive_filter_types"].default)
    b = FakeBackend()
    b.front = FrontWindow(pid=1, app_name="Slack", title=f"DM {EMAIL}")
    cw = MagicMock()
    cw.is_paused = False
    r = UiEventRecorder(database=db, capture_worker=cw, backend=b)
    r._clip_count = 0
    r._tick(None, 100.0)

    type_text(r, f"mail {EMAIL} or call {PHONE}", 101.0)
    r._tick(ctrl(KEY_ENTER, 104.0), 104.0)
    b.clip_count = 1
    b.clip_text = f"IBAN {IBAN}\nphone {PHONE}"
    r._tick(None, 105.0)
    r.flush_for_capture()

    conn = sqlite3.connect(db._db_path)
    rows = conn.execute("SELECT type, window_title, text FROM ui_events").fetchall()
    dump = repr(conn.execute("SELECT * FROM ui_events").fetchall())
    conn.close()

    types = {row[0] for row in rows}
    assert {"text", "clipboard"} <= types
    for secret in (EMAIL, PHONE, IBAN, "jane.doe", "555-0132", "0532 0130"):
        assert secret not in dump
    texts = {row[0]: row[2] for row in rows if row[2]}
    assert texts["text"] == "mail [REDACTED:email@example.com] or call [REDACTED:phone]"
    assert texts["clipboard"] == "IBAN [REDACTED:iban]\nphone [REDACTED:phone]"
