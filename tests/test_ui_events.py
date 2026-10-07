"""Tests for UI event capture: text grouping, recorder rules, storage, capture triggers."""

import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from screenmind.capture.ui_events.base import FrontWindow, PermissionStatus, UiEventBackend
from screenmind.capture.ui_events.models import (
    ElementInfo,
    EventType,
    KEY_BACKSPACE,
    KEY_CHAR,
    KEY_ENTER,
    KEY_TAB,
    RawEvent,
    UiEvent,
    format_user_actions,
)
from screenmind.capture.ui_events.recorder import PASSWORD_PLACEHOLDER, UiEventRecorder
from screenmind.capture.ui_events.text_buffer import TextBuffer
from screenmind.config import settings
from screenmind.storage.models import ScreenshotEntry


def key(c, ts=0.0):
    return RawEvent(kind="key", ts=ts, key=KEY_CHAR, char=c)


def ctrl(k, ts=0.0):
    return RawEvent(kind="key", ts=ts, key=k)


FIELD = ElementInfo(role="AXTextField", name="Message", pid=1)
OTHER_FIELD = ElementInfo(role="AXTextField", name="Search", pid=1)
PASSWORD = ElementInfo(role="AXTextField", subrole="AXSecureTextField", name="Password", pid=1, is_password=True)


# ── TextBuffer ──────────────────────────────────────────────────────


class TestTextBuffer:
    def _type(self, buf, text, target=FIELD, start=0.0):
        out = []
        for i, c in enumerate(text):
            out += buf.add(key(c, start + i * 0.1), target, "Slack", "general")
        return out

    def test_groups_chars_until_idle(self):
        buf = TextBuffer(idle_flush_s=2.0)
        assert self._type(buf, "hello") == []
        assert buf.poll(1.0) is None
        chunk = buf.poll(0.4 + 2.0)
        assert chunk.text == "hello"
        assert chunk.app_name == "Slack"
        assert chunk.target is FIELD

    def test_backspace_removes_last_char(self):
        buf = TextBuffer()
        self._type(buf, "helo")
        buf.add(ctrl(KEY_BACKSPACE, 1.0), FIELD, "Slack", None)
        self._type(buf, "lo", start=1.1)
        assert buf.flush().text == "hello"

    def test_enter_flushes(self):
        buf = TextBuffer()
        self._type(buf, "ship it")
        out = buf.add(ctrl(KEY_ENTER, 1.0), FIELD, "Slack", None)
        assert [c.text for c in out] == ["ship it"]
        assert not buf.active

    def test_tab_flushes(self):
        buf = TextBuffer()
        self._type(buf, "abc")
        assert [c.text for c in buf.add(ctrl(KEY_TAB), FIELD, "Slack", None)] == ["abc"]

    def test_focus_change_starts_new_chunk(self):
        buf = TextBuffer()
        self._type(buf, "one")
        out = self._type(buf, "two", target=OTHER_FIELD, start=1.0)
        assert [c.text for c in out] == ["one"]
        assert buf.flush().text == "two"

    def test_shortcut_flushes_and_is_not_text(self):
        buf = TextBuffer()
        self._type(buf, "abc")
        out = buf.add(RawEvent(kind="key", ts=1, key=KEY_CHAR, shortcut=True, shortcut_char="v"),
                      FIELD, "Slack", None)
        assert [c.text for c in out] == ["abc"]
        assert buf.flush() is None

    def test_max_chars_flushes(self):
        buf = TextBuffer(max_chars=5)
        out = self._type(buf, "abcdefg")
        assert [c.text for c in out] == ["abcde"]
        assert buf.flush().text == "fg"

    def test_all_deleted_gives_no_chunk(self):
        buf = TextBuffer()
        self._type(buf, "ab")
        buf.add(ctrl(KEY_BACKSPACE), FIELD, "Slack", None)
        buf.add(ctrl(KEY_BACKSPACE), FIELD, "Slack", None)
        assert buf.flush() is None

    def test_password_field_keeps_no_chars(self):
        buf = TextBuffer()
        self._type(buf, "hunter2", target=PASSWORD)
        assert buf._chars == []
        chunk = buf.flush()
        assert chunk.is_password
        assert chunk.text == ""


# ── Formatting ──────────────────────────────────────────────────────


class TestFormatting:
    def test_describe_click(self):
        e = UiEvent(timestamp=datetime.now(), type=EventType.CLICK, app_name="Slack",
                    element_role="AXButton", element_name="Send")
        assert e.describe() == 'clicked button "Send" in Slack'

    def test_format_user_actions_dedups_and_limits(self):
        rows = [{"type": "app_switch", "app_name": "Slack", "window_title": None,
                 "element_role": None, "element_name": None, "text": None}] * 3
        rows += [{"type": "text", "app_name": "Slack", "window_title": None,
                  "element_role": "AXTextArea", "element_name": None, "text": f"msg {i}"} for i in range(20)]
        out = format_user_actions(rows, max_lines=10)
        lines = out.split("\n")
        assert len(lines) == 10
        assert lines[-1] == '- typed "msg 19" in text area (Slack)'

    def test_format_user_actions_caps_size(self):
        rows = [{"type": "text", "app_name": "A", "window_title": None, "element_role": None,
                 "element_name": None, "text": "x" * 500 + str(i)} for i in range(10)]
        assert len(format_user_actions(rows, max_chars=600)) <= 600

    def test_format_empty(self):
        assert format_user_actions([]) is None


# ── Recorder ────────────────────────────────────────────────────────


class FakeBackend(UiEventBackend):
    name = "fake"

    def __init__(self):
        self.front = FrontWindow(pid=1, app_name="Slack", title="general")
        self.element = ElementInfo(role="AXButton", name="Send", pid=1)
        self.focus = FIELD
        self.clip_count = 0
        self.clip_text = None
        self.url = None

    def check_permissions(self):
        return PermissionStatus(True, True)

    def request_permissions(self):
        return PermissionStatus(True, True)

    def start(self, out):
        return True

    def stop(self):
        pass

    def is_running(self):
        return True

    def element_at(self, x, y):
        return self.element

    def focused_element(self):
        return self.focus

    def front_window(self):
        return self.front

    def app_name_for_pid(self, pid):
        return {1: "Slack", 2: "Safari"}.get(pid)

    def browser_url(self):
        return self.url

    def clipboard_change_count(self):
        return self.clip_count

    def read_clipboard(self):
        return self.clip_text


@pytest.fixture
def ui_settings(monkeypatch):
    monkeypatch.setattr(settings, "ui_events_enabled", True)
    monkeypatch.setattr(settings, "ui_events_types", "click,app_switch,text,clipboard")
    monkeypatch.setattr(settings, "event_triggered_capture", True)
    monkeypatch.setattr(settings, "blocked_apps", "")
    monkeypatch.setattr(settings, "sensitive_filter_enabled", True)
    monkeypatch.setattr(settings, "sensitive_filter_types", "credit_card,ssn,api_key,jwt,password")
    return settings


@pytest.fixture
def rec(ui_settings):
    backend = FakeBackend()
    db = MagicMock()
    cw = MagicMock()
    cw.is_paused = False
    r = UiEventRecorder(database=db, capture_worker=cw, backend=backend)
    r._clip_count = 0
    r._tick(None, 100.0)  # first poll sets the front window
    return r, backend, db, cw


def stored(db):
    events = []
    for call in db.insert_ui_events.call_args_list:
        events += call.args[0]
    return events


def type_text(r, text, start):
    for i, c in enumerate(text):
        r._tick(key(c, start + i * 0.05), start + i * 0.05)


class TestRecorder:
    def test_click_records_element_and_triggers_capture(self, rec):
        r, b, db, cw = rec
        r._tick(RawEvent(kind="mouse_down", ts=101.0, x=10.4, y=20.6), 101.0)
        r.flush_for_capture()
        [e] = stored(db)
        assert e.type == EventType.CLICK
        assert (e.app_name, e.element_role, e.element_name) == ("Slack", "AXButton", "Send")
        assert (e.x, e.y) == (10, 20)
        cw.request_capture.assert_called_once_with("click", 1.5)

    def test_click_in_text_field_does_not_trigger_capture(self, rec):
        r, b, db, cw = rec
        b.element = FIELD
        r._tick(RawEvent(kind="mouse_down", ts=101.0, x=1, y=1), 101.0)
        cw.request_capture.assert_not_called()

    def test_click_app_from_element_pid(self, rec):
        r, b, db, cw = rec
        b.element = ElementInfo(role="AXButton", name="Reload", pid=2)
        r._tick(RawEvent(kind="mouse_down", ts=101.0, x=1, y=1), 101.0)
        r.flush_for_capture()
        assert stored(db)[0].app_name == "Safari"

    def test_typing_becomes_one_text_event_after_pause(self, rec):
        r, b, db, cw = rec
        type_text(r, "deploy done", 101.0)
        r._tick(None, 101.6)
        assert not stored(db)
        r._tick(None, 104.0)  # > 2s idle
        r._write_pending()
        [e] = stored(db)
        assert e.type == EventType.TEXT
        assert e.text == "deploy done"
        assert e.element_name == "Message"
        cw.request_capture.assert_called_with("typing_pause", 0.5)

    def test_password_field_is_never_stored(self, rec):
        r, b, db, cw = rec
        b.focus = PASSWORD
        type_text(r, "hunter2", 101.0)
        r.flush_for_capture()
        [e] = stored(db)
        assert e.text == PASSWORD_PLACEHOLDER
        assert "hunter2" not in repr(e)

    def test_click_on_password_field_hides_value(self, rec):
        r, b, db, cw = rec
        b.element = ElementInfo(role="AXTextField", subrole="AXSecureTextField",
                                name="Password", value="secret", pid=1, is_password=True)
        r._tick(RawEvent(kind="mouse_down", ts=101.0, x=1, y=1), 101.0)
        r.flush_for_capture()
        [e] = stored(db)
        assert e.element_name == PASSWORD_PLACEHOLDER
        assert e.element_value is None

    def test_unknown_focus_records_no_text(self, rec):
        r, b, db, cw = rec
        b.focus = None
        type_text(r, "secret", 101.0)
        r.flush_for_capture()
        assert stored(db) == []

    def test_non_text_focus_records_no_text(self, rec):
        r, b, db, cw = rec
        b.focus = ElementInfo(role="AXWindow", pid=1)
        type_text(r, "jjjk", 101.0)
        r.flush_for_capture()
        assert stored(db) == []

    def test_sensitive_filter_applies_to_typed_text(self, rec):
        r, b, db, cw = rec
        type_text(r, "card 4111 1111 1111 1111", 101.0)
        r.flush_for_capture()
        [e] = stored(db)
        assert "4111" not in e.text

    def test_blocked_app_records_nothing(self, rec, monkeypatch):
        r, b, db, cw = rec
        monkeypatch.setattr(settings, "blocked_apps", "slack")
        r._tick(RawEvent(kind="mouse_down", ts=101.0, x=1, y=1), 101.0)
        type_text(r, "hello", 102.0)
        r.flush_for_capture()
        assert stored(db) == []

    def test_paused_capture_records_nothing(self, rec):
        r, b, db, cw = rec
        cw.is_paused = True
        r._tick(RawEvent(kind="mouse_down", ts=101.0, x=1, y=1), 101.0)
        type_text(r, "hello", 102.0)
        r.flush_for_capture()
        assert stored(db) == []

    def test_disabled_type_is_skipped(self, rec, monkeypatch):
        r, b, db, cw = rec
        monkeypatch.setattr(settings, "ui_events_types", "app_switch")
        r._tick(RawEvent(kind="mouse_down", ts=101.0, x=1, y=1), 101.0)
        type_text(r, "hello", 102.0)
        r.flush_for_capture()
        assert stored(db) == []

    def test_app_switch(self, rec):
        r, b, db, cw = rec
        type_text(r, "half", 100.1)
        b.front = FrontWindow(pid=2, app_name="Safari", title="Docs")
        r._tick(None, 101.0)
        r.flush_for_capture()
        events = stored(db)
        assert [e.type for e in events] == [EventType.TEXT, EventType.APP_SWITCH]
        assert events[0].text == "half"
        assert events[1].app_name == "Safari"
        cw.request_capture.assert_called_with("app_switch", 1.0)

    def test_window_focus_only_when_enabled(self, rec, monkeypatch):
        r, b, db, cw = rec
        b.front = FrontWindow(pid=1, app_name="Slack", title="random")
        r._tick(None, 101.0)
        monkeypatch.setattr(settings, "ui_events_types", "window_focus")
        b.front = FrontWindow(pid=1, app_name="Slack", title="dev")
        r._tick(None, 102.0)
        r.flush_for_capture()
        [e] = stored(db)
        assert e.type == EventType.WINDOW_FOCUS
        assert e.window_title == "dev"

    def test_click_in_browser_gets_url(self, rec):
        r, b, db, cw = rec
        b.url = "https://linear.app/team/issue/DIS-947"
        r._tick(RawEvent(kind="mouse_down", ts=101.0, x=1, y=1), 101.0)
        r.flush_for_capture()
        [e] = stored(db)
        assert e.url == "https://linear.app/team/issue/DIS-947"
        assert e.describe() == 'clicked button "Send" in Slack (linear.app)'

    def test_event_url_is_sanitized(self, rec):
        r, b, db, cw = rec
        b.url = "https://accounts.google.com/o/oauth2/auth?state=s&nonce=n"
        r._tick(RawEvent(kind="mouse_down", ts=101.0, x=1, y=1), 101.0)
        r.flush_for_capture()
        assert stored(db)[0].url == "https://accounts.google.com/"

    def test_click_in_other_app_gets_no_url(self, rec):
        r, b, db, cw = rec
        b.url = "https://linear.app/x"
        b.element = ElementInfo(role="AXButton", name="Reload", pid=2)  # Safari, not frontmost
        r._tick(RawEvent(kind="mouse_down", ts=101.0, x=1, y=1), 101.0)
        r.flush_for_capture()
        assert stored(db)[0].url is None

    def test_app_switch_gets_url(self, rec):
        r, b, db, cw = rec
        b.url = "https://docs.google.com/document/d/1"
        b.front = FrontWindow(pid=2, app_name="Safari", title="Plan")
        r._tick(None, 101.0)
        r.flush_for_capture()
        [e] = stored(db)
        assert e.url == "https://docs.google.com/document/d/1"
        assert e.describe() == "switched to Safari (docs.google.com): Plan"

    def test_browser_tab_switch_recorded_without_window_focus_type(self, rec):
        r, b, db, cw = rec
        b.url = "https://a.com/1"
        b.front = FrontWindow(pid=1, app_name="Slack", title="Page A")
        r._tick(None, 101.0)
        b.url = "https://b.com/2"
        b.front = FrontWindow(pid=1, app_name="Slack", title="Page B")
        r._tick(None, 102.0)
        r.flush_for_capture()
        events = stored(db)
        assert [(e.type, e.url) for e in events] == [
            (EventType.WINDOW_FOCUS, "https://a.com/1"),
            (EventType.WINDOW_FOCUS, "https://b.com/2"),
        ]
        cw.request_capture.assert_called_with("page_change", 1.0)

    def test_browser_title_change_same_url_not_recorded(self, rec):
        r, b, db, cw = rec
        b.url = "https://app.slack.com/client"
        b.front = FrontWindow(pid=1, app_name="Slack", title="Slack")
        r._tick(None, 101.0)
        b.front = FrontWindow(pid=1, app_name="Slack", title="(3) Slack")
        r._tick(None, 102.0)
        r.flush_for_capture()
        assert len(stored(db)) == 1

    def test_non_browser_title_change_not_recorded_by_default(self, rec):
        r, b, db, cw = rec
        b.front = FrontWindow(pid=1, app_name="Slack", title="random")
        r._tick(None, 101.0)
        r.flush_for_capture()
        assert stored(db) == []

    def test_clipboard_change(self, rec):
        r, b, db, cw = rec
        b.clip_count = 1
        b.clip_text = "  copied text  "
        r._tick(None, 102.0)
        r.flush_for_capture()
        [e] = stored(db)
        assert (e.type, e.text) == (EventType.CLIPBOARD, "copied text")

    def test_secret_clipboard_skipped(self, rec):
        r, b, db, cw = rec
        b.clip_count = 1
        b.clip_text = None  # backend returns None for concealed pasteboard types
        r._tick(None, 102.0)
        r.flush_for_capture()
        assert stored(db) == []

    def test_no_trigger_when_event_capture_off(self, rec, monkeypatch):
        r, b, db, cw = rec
        monkeypatch.setattr(settings, "event_triggered_capture", False)
        r._tick(RawEvent(kind="mouse_down", ts=101.0, x=1, y=1), 101.0)
        cw.request_capture.assert_not_called()

    def test_unsupported_platform(self, ui_settings):
        r = UiEventRecorder(database=MagicMock(), backend=None)
        assert r.start() is False
        assert r.status()["supported"] is False


# ── Database ────────────────────────────────────────────────────────


def _ev(ts, type_=EventType.CLICK, **kw):
    return UiEvent(timestamp=ts, type=type_, app_name=kw.pop("app_name", "Slack"), **kw)


class TestDatabase:
    def test_tables_exist(self, db):
        conn = db._get_conn()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(activities)")]
        assert "user_actions" in cols
        conn.execute("SELECT * FROM ui_events LIMIT 0")

    def test_url_column_roundtrip(self, db):
        t0 = datetime(2026, 10, 6, 10, 0, 0)
        db.insert_ui_events([_ev(t0, url="https://linear.app/x")])
        rows = db.get_ui_events_range(t0.isoformat(), t0.isoformat())
        assert rows[0]["url"] == "https://linear.app/x"

    def test_insert_attach_get(self, db):
        t0 = datetime(2026, 10, 6, 10, 0, 0)
        db.insert_ui_events([
            _ev(t0, element_name="Send"),
            _ev(t0 + timedelta(seconds=5), EventType.TEXT, text="hi"),
            _ev(t0 + timedelta(seconds=30), element_name="Later"),
        ])
        aid = db.insert_activity(ScreenshotEntry(timestamp=t0 + timedelta(seconds=10), screenshot_path="x"))
        assert db.attach_ui_events(aid, t0 + timedelta(seconds=10)) == 2
        rows = db.get_ui_events(aid)
        assert [r["type"] for r in rows] == ["click", "text"]
        # Already-linked events are not moved to the next frame
        aid2 = db.insert_activity(ScreenshotEntry(timestamp=t0 + timedelta(seconds=40), screenshot_path="y"))
        assert db.attach_ui_events(aid2, t0 + timedelta(seconds=40)) == 1

    def test_range_query(self, db):
        t0 = datetime(2026, 10, 6, 10, 0, 0)
        db.insert_ui_events([_ev(t0), _ev(t0 + timedelta(minutes=5), EventType.TEXT, text="x")])
        rows = db.get_ui_events_range(t0.isoformat(), (t0 + timedelta(minutes=10)).isoformat(), event_type="text")
        assert len(rows) == 1 and rows[0]["text"] == "x"

    def test_user_actions_are_searchable(self, db):
        aid = db.insert_activity(ScreenshotEntry(timestamp=datetime.now(), screenshot_path="x"))
        db.set_user_actions(aid, '- typed "quarterly roadmap draft" in text area (Notes)')
        hits = db._get_conn().execute(
            "SELECT rowid FROM activities_fts WHERE activities_fts MATCH ?", ("roadmap",)
        ).fetchall()
        assert [h[0] for h in hits] == [aid]

    def test_retention_deletes_old_events(self, db):
        old = datetime.now() - timedelta(days=30)
        db.insert_ui_events([_ev(old), _ev(datetime.now())])
        db.cleanup_old_data(7)
        assert db._get_conn().execute("SELECT COUNT(*) FROM ui_events").fetchone()[0] == 1

    def test_deleting_activity_unlinks_events(self, db):
        t0 = datetime.now()
        db.insert_ui_events([_ev(t0)])
        aid = db.insert_activity(ScreenshotEntry(timestamp=t0, screenshot_path="x"))
        db.attach_ui_events(aid, t0)
        conn = db._get_conn()
        conn.execute("DELETE FROM activities WHERE id = ?", (aid,))
        conn.commit()
        assert conn.execute("SELECT activity_id FROM ui_events").fetchone()[0] is None


# ── Capture worker triggers ─────────────────────────────────────────


@pytest.fixture
def worker(mock_settings):
    with patch("screenmind.workers.capture_worker.ScreenCapture"):
        from screenmind.workers.capture_worker import CaptureWorker
        import asyncio
        w = CaptureWorker(queue=asyncio.Queue())
        w._paused = False
        yield w


class TestCaptureTriggers:
    def test_trigger_due_after_delay(self, worker):
        worker.request_capture("click", delay=0.0)
        assert worker._take_due_trigger() == "click"
        assert worker._take_due_trigger() is None

    def test_trigger_not_due_yet(self, worker):
        worker.request_capture("click", delay=60)
        assert worker._take_due_trigger() is None

    def test_rate_limit_postpones(self, worker):
        worker._last_save_time = time.time()
        worker.request_capture("click", delay=0.0)
        assert worker._take_due_trigger() is None
        due = worker._pending_trigger[0]
        assert due >= worker._last_save_time + 2.9

    def test_burst_is_merged_but_bounded(self, worker):
        worker.request_capture("click", delay=1.5)
        first = worker._pending_trigger[1]
        for _ in range(5):
            worker.request_capture("click", delay=10)
        assert worker._pending_trigger[0] <= first + 3.0 + 1e-6

    def test_paused_ignores_requests(self, worker):
        worker._paused = True
        worker.request_capture("click", delay=0.0)
        assert worker._pending_trigger is None

    async def test_link_ui_events_sets_user_actions(self, worker, db, monkeypatch):
        monkeypatch.setattr(settings, "ui_events_enabled", True)
        t0 = datetime.now() - timedelta(seconds=5)
        db.insert_ui_events([_ev(t0, element_role="AXButton", element_name="Send")])
        aid = db.insert_activity(ScreenshotEntry(timestamp=datetime.now(), screenshot_path="x"))
        worker._db = db
        worker._ui_recorder = MagicMock()
        text = await worker._link_ui_events(aid, datetime.now())
        worker._ui_recorder.flush_for_capture.assert_called_once()
        assert text == '- clicked button "Send" in Slack'
        assert db.get_activity_by_id(aid)["user_actions"] == text


# ── Analyzer ────────────────────────────────────────────────────────


class TestAnalyzerHint:
    def test_user_actions_in_prompt(self):
        from screenmind.engine.analyzer import GemmaAnalyzer, USER_ACTIONS_HINT
        a = GemmaAnalyzer()
        a._initialized = True
        with patch("screenmind.engine.analyzer.llm_client.chat_with_images", return_value="{}") as chat:
            a.analyze_screenshot_balanced(Image.new("RGB", (32, 32)), user_actions='- clicked button "Send" in Slack')
        prompt = chat.call_args.kwargs["prompt"]
        assert USER_ACTIONS_HINT in prompt
        assert 'clicked button "Send"' in prompt


def test_format_skips_unnamed_clicks():
    rows = [
        {"type": "click", "app_name": "Slack", "window_title": None, "element_role": "AXGroup",
         "element_name": None, "text": None},
        {"type": "click", "app_name": "Slack", "window_title": None, "element_role": "AXButton",
         "element_name": "Send", "text": None},
    ]
    assert format_user_actions(rows) == '- clicked button "Send" in Slack'

