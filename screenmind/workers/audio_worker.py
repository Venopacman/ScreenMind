"""
Audio Worker — Call Tracking
Detects calls from every visible window (and, on macOS, which apps hold the
mic) and records each call's start and end, app, window title and room URL
in the meetings table. It records no audio. Transcripts and summaries of
older calls, made when ScreenMind still transcribed with Gemma, stay in the
table and still show.
"""

import logging
import time
import threading
from datetime import datetime
from typing import Optional

from screenmind.config import settings
from screenmind.storage.database import Database
from screenmind.workers.call_detection import CallMatch, match_call, owner_on_mic, same_call

logger = logging.getLogger("screenmind.workers.audio_worker")


def _clean(text: Optional[str]) -> Optional[str]:
    """The sensitive-data filter, as for window titles and screen text."""
    from screenmind.workers.capture_worker import filter_sensitive
    return filter_sensitive(text)


# Call detection runs on its own thread at this interval. It must not share
# the capture loop: a slow screen grab there (30 s+) starves the check.
CHECK_INTERVAL_S = 5
# A call starts after this many detections in a row (CHECK_INTERVAL_S apart). Filters
# out a window flashing past and short mic use like Slack voice clips.
CONFIRM_CHECKS = 2
# A call ends when neither its window nor its mic use was seen for
# settings.call_end_grace_s. Covers switching browser tabs away from Meet
# while muted. The end time saved is the last time the call was seen, not
# when the grace ran out. A call that starts again within the grace of the
# last row's end, and is the same call (same_call), resumes that row: this
# keeps one row across a restart of ScreenMind.
# While a call runs, save its duration this often so a crash keeps it.
DURATION_SAVE_EVERY_S = 60
# How often to retry reading the room URL when the first try failed.
URL_RETRY_EVERY_S = 30


class AudioWorker:
    """
    Background worker for call tracking.
    - Detects calls from all visible windows, matching owner app and title,
      plus mic use where the OS reports it (see call_detection.py)
    - Records start/end, app, window title and room URL for every call
    """

    def __init__(self, database: Database):
        self._db = database
        self._running = False
        self._lock = threading.RLock()
        self._detect_thread: Optional[threading.Thread] = None
        self._detect_stop = threading.Event()

        # Call state
        self._in_meeting = False
        self._meeting_id: Optional[int] = None
        self._meeting_app: Optional[str] = None
        self._meeting_owner: Optional[str] = None  # window owner / mic app, for keep-alive
        self._meeting_title: Optional[str] = None
        self._meeting_url: Optional[str] = None
        self._session_start: Optional[datetime] = None
        self._last_seen: float = 0
        self._last_duration_save: float = 0
        self._last_url_try: float = 0

        # Candidate call, waiting for CONFIRM_CHECKS detections
        self._pending: Optional[CallMatch] = None
        self._pending_count = 0
        self._pending_since: float = 0

    @property
    def in_meeting(self) -> bool:
        return self._in_meeting

    # ── Call detection ───────────────────────────────────────────────

    def start(self):
        """Start the call detection thread. It runs while capture is paused too."""
        if self._detect_thread and self._detect_thread.is_alive():
            return
        self._detect_stop.clear()
        self._detect_thread = threading.Thread(
            target=self._detect_loop, name="call-detection", daemon=True)
        self._detect_thread.start()

    def stop(self):
        """Stop detection and end the current call (shutdown)."""
        self._detect_stop.set()
        self.force_stop()

    def _detect_loop(self):
        while not self._detect_stop.wait(CHECK_INTERVAL_S):
            try:
                self.check_calls()
            except Exception as e:
                logger.debug(f"Call detection failed: {e}")

    def check_calls(self):
        """One detection pass: read every visible window and the apps using
        the mic, then advance the call state machine."""
        from screenmind.capture.window import list_visible_windows, get_mic_apps
        if not settings.meeting_apps_list:
            return
        self.update(list_visible_windows(), get_mic_apps())

    def update(self, windows: list, mic_apps: Optional[set], now: Optional[float] = None):
        """Advance the call state machine with one snapshot of the screen."""
        now = time.time() if now is None else now
        match = match_call(windows, mic_apps, settings.meeting_apps_list)
        with self._lock:
            if self._in_meeting:
                if match and match.app == self._meeting_app:
                    self._seen(match, now)
                    return
                if owner_on_mic(self._meeting_owner, mic_apps):
                    self._seen(None, now)
                    return
                if match:
                    # A different call took over: close this one, confirm the new one
                    logger.info(f"Call switched from {self._meeting_app} to {match.app}")
                    self._stop_meeting(end=self._last_seen)
                elif now - self._last_seen > settings.call_end_grace_s:
                    self._stop_meeting(end=self._last_seen)
                    return
                else:
                    return

            if not match:
                self._pending = None
                self._pending_count = 0
                return
            if self._pending and self._pending.app == match.app:
                self._pending_count += 1
            else:
                self._pending = match
                self._pending_count = 1
                self._pending_since = now
            if self._pending_count >= CONFIRM_CHECKS:
                since = self._pending_since
                self._pending = None
                self._pending_count = 0
                self._start_meeting(match, start=since, now=now)

    def _seen(self, match: Optional[CallMatch], now: float):
        """The current call is still running."""
        self._last_seen = now
        title = url = duration = None
        if match and match.title and not self._meeting_title:
            title = self._meeting_title = _clean(match.title)
        if match and match.is_browser and not self._meeting_url \
                and now - self._last_url_try >= URL_RETRY_EVERY_S:
            url = self._meeting_url = self._read_url(match, now)
        if now - self._last_duration_save >= DURATION_SAVE_EVERY_S:
            self._last_duration_save = now
            duration = self._minutes(now)
        if self._meeting_id and (title or url or duration is not None):
            try:
                self._db.update_meeting_call_info(
                    self._meeting_id, window_title=title, url=url, duration_minutes=duration)
            except Exception as e:
                logger.warning(f"Could not update meeting {self._meeting_id}: {e}")

    def _read_url(self, match: CallMatch, now: float) -> Optional[str]:
        """Room URL of a browser call window, sanitized like other stored URLs."""
        self._last_url_try = now
        from screenmind.capture.window import get_window_url
        from screenmind.privacy.url_filter import sanitize_url
        return sanitize_url(get_window_url(match.pid, match.bounds))

    def _minutes(self, now: float) -> float:
        if not self._session_start:
            return 0
        return round(max(0.0, now - self._session_start.timestamp()) / 60, 1)

    def _start_meeting(self, match: CallMatch, start: float, now: float):
        """Begin tracking a call."""
        self._in_meeting = True
        self._meeting_app = match.app
        self._meeting_owner = match.owner
        self._meeting_title = _clean(match.title)
        self._meeting_url = self._read_url(match, now) if match.is_browser else None
        self._session_start = datetime.fromtimestamp(start)
        self._last_seen = now
        self._last_duration_save = now

        if self._resume_recent(match, start):
            return
        self._meeting_id = self._db.insert_meeting(
            start_time=self._session_start,
            app_name=match.app,
            transcript=None,
            summary=None,
            window_title=self._meeting_title,
            url=self._meeting_url,
        )
        logger.info(f"Call started ({match.app}"
                    f"{', ' + self._meeting_title if self._meeting_title else ''})")

    def _resume_recent(self, match: CallMatch, start: float) -> bool:
        """Continue the last meetings row if it is this call and it ended
        less than the grace before this start (a restart of ScreenMind).
        Only rows tracked without a transcript: an old recorded row keeps
        its own transcript and summary."""
        try:
            row = self._db.get_latest_meeting()
            if not row or not row.get("end_time") or row.get("transcript") is not None:
                return False
            ended = datetime.fromisoformat(row["end_time"]).timestamp()
            if not (0 <= start - ended <= settings.call_end_grace_s):
                return False
            # Stored titles are filtered, so compare the filtered title.
            title = _clean(match.title)
            if not same_call(match.app, self._meeting_url, title, match.is_browser, row):
                return False
            self._db.reopen_meeting(row["id"])
            self._db.update_meeting_call_info(
                row["id"], window_title=title, url=self._meeting_url)
        except Exception as e:
            logger.warning(f"Could not resume the last call: {e}")
            return False
        self._meeting_id = row["id"]
        self._session_start = datetime.fromisoformat(row["start_time"])
        self._meeting_title = row.get("window_title") or title
        self._meeting_url = row.get("url") or self._meeting_url
        logger.info(f"Call resumed ({match.app}, row {row['id']}, "
                    f"gap {start - ended:.0f}s)")
        return True

    def _stop_meeting(self, end: Optional[float] = None):
        """End the call and save its end time and duration."""
        with self._lock:
            if not self._in_meeting:
                return
            self._in_meeting = False
            meeting_id = self._meeting_id
            end_time = datetime.fromtimestamp(end) if end else datetime.now()
            duration = round(max(0.0, (end_time - self._session_start).total_seconds()) / 60, 1) \
                if self._session_start else 0
            app = self._meeting_app

            self._meeting_id = None
            self._meeting_app = None
            self._meeting_owner = None
            self._meeting_title = None
            self._meeting_url = None
            self._session_start = None

        logger.info(f"Call ended ({app}, {duration:.1f} min)")
        if meeting_id:
            self._db.end_meeting(meeting_id, end_time, duration)

    def force_stop(self):
        """End the current call (e.g., on shutdown or delete) at the last
        time it was seen, so a restart can resume it (_resume_recent)."""
        if self._in_meeting:
            self._stop_meeting(end=self._last_seen or None)

    @property
    def stats(self) -> dict:
        return {
            "tracking": bool(settings.meeting_apps_list),
            "in_meeting": self._in_meeting,
            "meeting_app": self._meeting_app,
            "meeting_title": self._meeting_title,
            "meeting_url": self._meeting_url,
        }
