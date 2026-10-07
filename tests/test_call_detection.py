"""Call detection: rules on visible windows + mic use, and the call state
machine that writes start/end rows into the meetings table."""

from datetime import datetime
from unittest.mock import patch

import pytest

from screenmind.config import settings
from screenmind.platform_support.macos_audio import app_name_from_path
from screenmind.storage.database import Database
from screenmind.workers import audio_worker as aw
from screenmind.workers.call_detection import match_call

KEYWORDS = ["zoom", "teams", "meet", "webex", "slack", "discord"]


def win(owner, title, pid=1, bounds=(0, 0, 800, 600)):
    return {"owner": owner, "title": title, "pid": pid, "bounds": bounds}


# Real window titles seen on 2026-10-06
FOCUSED_CLAUDE = win("Claude", "Claude", pid=10)
MEET_DAILY = win("Google Chrome", "Meet - distillery daily #2 🔊", pid=20, bounds=(1512, 0, 2198, 1257))
SLACK_CHANNEL = win("Slack", "distillery-dev-team - Nebius - Slack", pid=30)


class TestMatchCall:
    def test_meet_on_other_display_behind_focused_window(self):
        m = match_call([FOCUSED_CLAUDE, MEET_DAILY], None, KEYWORDS)
        assert m.app == "Google Meet"
        assert m.title == "Meet - distillery daily #2 🔊"
        assert m.is_browser and m.pid == 20 and m.bounds == (1512, 0, 2198, 1257)

    def test_meet_title_on_windows_has_browser_suffix(self):
        m = match_call([win("chrome", "Meet - Nebius Academy Tech - Google Chrome")], None, KEYWORDS)
        assert m and m.app == "Google Meet"

    @pytest.mark.parametrize("title", [
        "Choose what to share with meet.google.com",  # screen-share picker
        "Google Meet",                                 # landing page
        "Meeting notes - Google Docs",
    ])
    def test_meet_non_call_titles(self, title):
        assert match_call([win("Google Chrome", title)], None, KEYWORDS) is None

    def test_meet_title_outside_a_browser_is_ignored(self):
        assert match_call([win("Notes", "Meet - agenda for Tuesday")], None, KEYWORDS) is None

    def test_slack_open_all_day_is_not_a_call(self):
        assert match_call([SLACK_CHANNEL], set(), KEYWORDS) is None
        assert match_call([SLACK_CHANNEL], None, KEYWORDS) is None

    def test_slack_huddle_from_mic_use(self):
        m = match_call([FOCUSED_CLAUDE, SLACK_CHANNEL], {"slack"}, KEYWORDS)
        assert m.app == "Slack" and m.owner == "Slack"

    def test_slack_huddle_with_no_visible_window(self):
        m = match_call([FOCUSED_CLAUDE], {"slack", "granola"}, KEYWORDS)
        assert m.app == "Slack" and m.title is None

    def test_slack_huddle_window_title(self):
        m = match_call([win("slack", "Huddle: distillery-dev-team")], None, KEYWORDS)
        assert m and m.app == "Slack"

    def test_browser_on_mic_alone_is_not_a_call(self):
        assert match_call([win("Google Chrome", "HAL review · full run")], {"google chrome"}, KEYWORDS) is None

    def test_zoom_meeting_window(self):
        assert match_call([win("zoom.us", "Zoom Meeting")], None, KEYWORDS).app == "Zoom"
        assert match_call([win("zoom.us", "Zoom Workplace")], set(), KEYWORDS) is None

    def test_teams(self):
        assert match_call([win("Microsoft Teams", "Meeting with Ann | Microsoft Teams")], None, KEYWORDS).app \
            == "Microsoft Teams"
        assert match_call([win("Microsoft Teams", "Calendar | Microsoft Teams")], set(), KEYWORDS) is None
        assert match_call([win("Microsoft Teams", "Daily | Microsoft Teams")], {"microsoft teams"}, KEYWORDS).app \
            == "Microsoft Teams"

    def test_discord_needs_mic(self):
        d = win("Discord", "#general | Friends - Discord")
        assert match_call([d], set(), KEYWORDS) is None
        assert match_call([d], {"discord"}, KEYWORDS).app == "Discord"

    def test_user_added_keyword_matches_owner_or_title(self):
        m = match_call([win("FaceTime", "FaceTime")], None, KEYWORDS + ["facetime"])
        assert m and m.app == "FaceTime"
        m = match_call([win("Firefox", "Jitsi Meet | room42")], None, ["jitsi"])
        assert m and m.app == "Firefox"

    def test_no_keywords_no_calls(self):
        assert match_call([MEET_DAILY], {"slack"}, []) is None


def test_app_name_from_helper_path():
    assert app_name_from_path(
        "/Applications/Slack.app/Contents/Frameworks/Slack Helper.app/Contents/MacOS/Slack Helper") == "Slack"
    assert app_name_from_path("/Applications/zoom.us.app/Contents/MacOS/zoom.us") == "zoom.us"
    assert app_name_from_path("/usr/libexec/avconferenced") is None
    assert app_name_from_path(None) is None


# ── State machine ──────────────────────────────────────────────────────

T0 = datetime(2026, 10, 6, 16, 31, 0).timestamp()


@pytest.fixture
def db(tmp_path):
    return Database(db_path=tmp_path / "test.db")


@pytest.fixture
def worker(db, monkeypatch):
    monkeypatch.setattr(settings, "meeting_transcription", False)
    monkeypatch.setattr(settings, "meeting_apps", ",".join(KEYWORDS))
    with patch.object(aw.AudioWorker, "_init_transcription"), \
            patch("screenmind.capture.window.get_window_url",
                  return_value="https://meet.google.com/dsv-einb-rbg?authuser=0&pli=1"):
        yield aw.AudioWorker(database=db)


def rows(db):
    conn = db._get_conn()
    return [dict(r) for r in conn.execute("SELECT * FROM meetings ORDER BY id")]


def tick(worker, t, windows, mic=None):
    worker.update(windows, mic, now=T0 + t)


class TestCallTracking:
    def test_meet_call_tracked_without_transcription(self, worker, db):
        tick(worker, 0, [FOCUSED_CLAUDE, MEET_DAILY])
        assert rows(db) == []  # one sighting is not enough
        tick(worker, 5, [FOCUSED_CLAUDE, MEET_DAILY])
        [r] = rows(db)
        assert r["app_name"] == "Google Meet"
        assert r["window_title"] == "Meet - distillery daily #2 🔊"
        assert r["url"] == "https://meet.google.com/dsv-einb-rbg"  # query dropped
        assert r["start_time"] == datetime.fromtimestamp(T0).isoformat()
        assert r["end_time"] is None and r["transcript"] is None
        assert worker.stats["in_meeting"] and not worker.stats["recording"]

        # Meet window hidden for 60s (inside the grace), then back
        tick(worker, 60, [FOCUSED_CLAUDE])
        tick(worker, 100, [FOCUSED_CLAUDE, MEET_DAILY])
        assert rows(db)[0]["end_time"] is None

        # Call over: window gone. Ends only after the grace, at the last sighting.
        tick(worker, 105, [FOCUSED_CLAUDE])
        tick(worker, 100 + aw.END_GRACE_S, [FOCUSED_CLAUDE])
        assert rows(db)[0]["end_time"] is None
        tick(worker, 101 + aw.END_GRACE_S, [FOCUSED_CLAUDE])
        [r] = rows(db)
        assert r["end_time"] == datetime.fromtimestamp(T0 + 100).isoformat()
        assert r["duration_minutes"] == pytest.approx(1.7, abs=0.05)
        assert r["transcript"] is None and r["summary"] is None
        assert not worker.in_meeting

    def test_single_flash_does_not_start_a_call(self, worker, db):
        tick(worker, 0, [MEET_DAILY])
        tick(worker, 5, [FOCUSED_CLAUDE])
        tick(worker, 10, [MEET_DAILY])
        assert rows(db) == []

    def test_mic_keeps_browser_call_alive_after_tab_switch(self, worker, db):
        tick(worker, 0, [MEET_DAILY], {"google chrome"})
        tick(worker, 5, [MEET_DAILY], {"google chrome"})
        other_tab = win("Google Chrome", "HAL review · full run", pid=20)
        tick(worker, 10, [other_tab], {"google chrome"})
        tick(worker, 300, [other_tab], {"google chrome"})
        assert worker.in_meeting
        tick(worker, 305, [other_tab], set())  # left the call: mic released
        assert worker.in_meeting
        tick(worker, 301 + aw.END_GRACE_S, [other_tab], set())
        [r] = rows(db)
        assert r["end_time"] == datetime.fromtimestamp(T0 + 300).isoformat()

    def test_slack_huddle_start_and_end(self, worker, db):
        for t in (0, 5, 10):
            tick(worker, t, [FOCUSED_CLAUDE, SLACK_CHANNEL], {"slack"})
        tick(worker, 15, [FOCUSED_CLAUDE, SLACK_CHANNEL], set())
        tick(worker, 16 + aw.END_GRACE_S, [FOCUSED_CLAUDE, SLACK_CHANNEL], set())
        [r] = rows(db)
        assert r["app_name"] == "Slack" and r["url"] is None
        assert r["window_title"] == "distillery-dev-team - Nebius - Slack"
        assert r["end_time"] == datetime.fromtimestamp(T0 + 10).isoformat()

    def test_switching_to_another_call_closes_the_first(self, worker, db):
        tick(worker, 0, [MEET_DAILY])
        tick(worker, 5, [MEET_DAILY])
        tick(worker, 10, [SLACK_CHANNEL], {"slack"})
        tick(worker, 15, [SLACK_CHANNEL], {"slack"})
        first, second = rows(db)
        assert first["app_name"] == "Google Meet"
        assert first["end_time"] == datetime.fromtimestamp(T0 + 5).isoformat()
        assert second["app_name"] == "Slack" and second["end_time"] is None

    def test_duration_saved_while_running_and_used_after_crash(self, worker, db):
        tick(worker, 0, [MEET_DAILY])
        tick(worker, 5, [MEET_DAILY])
        tick(worker, 5 + aw.DURATION_SAVE_EVERY_S, [MEET_DAILY])
        assert rows(db)[0]["duration_minutes"] == pytest.approx(1.1, abs=0.05)
        # App crashed: next start closes the row from the saved duration
        assert db.cleanup_stale_meetings() == 1
        [r] = rows(db)
        assert r["end_time"] is not None and r["end_time"] > r["start_time"]
        assert r["summary"] is None  # tracked-only rows get no "interrupted" summary

    def test_force_stop_ends_call(self, worker, db):
        tick(worker, 0, [MEET_DAILY])
        tick(worker, 5, [MEET_DAILY])
        worker.force_stop()
        assert rows(db)[0]["end_time"] is not None
        assert not worker.in_meeting

    def test_recording_starts_only_with_transcription_and_audio_model(self, worker, db, monkeypatch):
        monkeypatch.setattr(settings, "meeting_transcription", True)
        worker._available = True
        with patch("screenmind.engine.model_manager.is_audio_capable", return_value=False):
            tick(worker, 0, [MEET_DAILY])
            tick(worker, 5, [MEET_DAILY])
        assert worker.in_meeting and not worker.stats["recording"]
        assert rows(db)[0]["transcript"] is None


def test_migration_v10_adds_meeting_columns(db):
    conn = db._get_conn()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(meetings)")}
    assert {"window_title", "url"} <= cols
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] >= 10


class TestAxPageUrl:
    """MacOSAdapter._ax_page_url on a fake AX tree (dicts as elements)."""

    @staticmethod
    def adapter():
        from screenmind.platform_support.macos import MacOSAdapter
        a = MacOSAdapter.__new__(MacOSAdapter)
        a._ax_attr = lambda el, attr: el.get(attr)
        return a

    @staticmethod
    def area(url, children=()):
        return {"AXRole": "AXWebArea", "AXURL": url, "AXChildren": list(children)}

    def test_skips_devtools_and_iframes(self):
        page = self.area("https://meet.google.com/dsv-einb-rbg",
                         [self.area("https://accounts.google.com/frame")])
        window = {"AXRole": "AXWindow", "AXChildren": [
            {"AXRole": "AXGroup", "AXChildren": [self.area("devtools://devtools/bundled/inspector.html")]},
            {"AXRole": "AXGroup", "AXChildren": [page]},
        ]}
        assert self.adapter()._ax_page_url(window) == "https://meet.google.com/dsv-einb-rbg"

    def test_ambiguous_or_missing_gives_none(self):
        two = {"AXRole": "AXWindow", "AXChildren": [self.area("https://a.example/"), self.area("https://b.example/")]}
        assert self.adapter()._ax_page_url(two) is None
        assert self.adapter()._ax_page_url({"AXRole": "AXWindow", "AXChildren": []}) is None


def test_detection_thread_runs_without_capture_loop(worker, monkeypatch):
    """Call checks must not depend on the capture loop, which can stall for
    30 s+ on a slow screen grab."""
    import threading
    monkeypatch.setattr(aw, "CHECK_INTERVAL_S", 0.01)
    ran = threading.Event()
    monkeypatch.setattr(worker, "check_calls", ran.set)
    worker.start()
    try:
        assert ran.wait(2)
    finally:
        worker.stop()
    worker._detect_thread.join(2)
    assert not worker._detect_thread.is_alive()
