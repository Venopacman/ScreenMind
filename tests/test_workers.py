"""Comprehensive tests for capture and analysis workers."""
import pytest
import asyncio
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

from screenmind.workers.capture_worker import CaptureWorker, CaptureResult
import screenmind.config as settings_mod


class TestCaptureWorker:
    """Tests for the capture worker."""

    def _make_worker(self):
        queue = asyncio.Queue(maxsize=100)
        return CaptureWorker(queue=queue), queue

    def test_starts_paused(self):
        worker, _ = self._make_worker()
        assert worker.is_paused is True
        assert worker._running is False

    def test_pause_resume(self):
        worker, _ = self._make_worker()
        worker.resume(source="test")
        assert worker.is_paused is False
        worker.pause(source="test")
        assert worker.is_paused is True

    def test_stats_keys(self):
        worker, _ = self._make_worker()
        stats = worker.stats
        assert "running" in stats
        assert "paused" in stats
        assert "captures" in stats
        assert "skipped" in stats

    def test_stop_sets_running_false(self):
        worker, _ = self._make_worker()
        worker._running = True
        worker.stop()
        assert worker._running is False

    def test_pause_resets_dedup(self):
        """Pausing resets the dedup hash so next capture is always fresh."""
        worker, _ = self._make_worker()
        worker._paused = False  # Must be unpaused for pause() to run (idempotent guard)
        worker._dedup._last_hash = "something"
        worker.pause(source="test")
        assert worker._dedup._last_hash is None

    def test_initial_counts_zero(self):
        worker, _ = self._make_worker()
        assert worker._capture_count == 0
        assert worker._skip_count == 0
        assert worker._consecutive_skips == 0


class TestCaptureResult:
    """Tests for CaptureResult dataclass."""

    def test_create_basic(self, tmp_path):
        result = CaptureResult(
            filepath=tmp_path / "test.jpg",
            timestamp=datetime.now(),
            window_title="Test Window",
            app_name="TestApp",
        )
        assert result.app_name == "TestApp"
        assert result.activity_id is None
        assert result.a11y_text is None
        assert result.phash is None

class TestAnalysisWorkerStats:
    """Tests for analysis worker state management."""

    def test_flush_queue(self):
        """flush_queue drains all items."""
        from screenmind.workers.analysis_worker import AnalysisWorker

        queue = asyncio.Queue(maxsize=100)
        db = MagicMock()
        worker = AnalysisWorker(queue=queue, database=db)

        # Add some items
        for i in range(5):
            queue.put_nowait(MagicMock())

        assert queue.qsize() == 5
        worker.flush_queue()
        assert queue.qsize() == 0

    def test_stats_keys(self):
        from screenmind.workers.analysis_worker import AnalysisWorker

        queue = asyncio.Queue(maxsize=100)
        db = MagicMock()
        worker = AnalysisWorker(queue=queue, database=db)

        stats = worker.stats
        assert "running" in stats
        assert "processed" in stats
        assert "errors" in stats
        assert "queue_size" in stats
        assert "cache_hits" in stats
        assert "cache_size" in stats

    def test_initial_state(self):
        from screenmind.workers.analysis_worker import AnalysisWorker

        queue = asyncio.Queue(maxsize=100)
        db = MagicMock()
        worker = AnalysisWorker(queue=queue, database=db)

        assert worker._processed == 0
        assert worker._errors == 0
        assert worker._cache_hits == 0
        assert len(worker._app_cache) == 0

    def test_stop(self):
        from screenmind.workers.analysis_worker import AnalysisWorker

        queue = asyncio.Queue(maxsize=100)
        db = MagicMock()
        worker = AnalysisWorker(queue=queue, database=db)
        worker._running = True
        worker.stop()
        assert worker._running is False


class TestURLExtraction:
    """Tests for URL extraction in analysis worker."""

    def test_extract_url_basic(self):
        from screenmind.workers.analysis_worker import _extract_url
        assert _extract_url("Visit https://github.com/user/repo today") == "https://github.com/user/repo"

    def test_extract_url_none_for_empty(self):
        from screenmind.workers.analysis_worker import _extract_url
        assert _extract_url("") is None
        assert _extract_url("no urls here") is None

    def test_extract_url_filters_noise(self):
        from screenmind.workers.analysis_worker import _extract_url
        # localhost and CDN URLs should be filtered
        assert _extract_url("http://localhost:3000/api") is None
        assert _extract_url("https://cdn.example.com/file.js") is None

    def test_extract_all_urls(self):
        from screenmind.workers.analysis_worker import _extract_all_urls
        text = "Check https://github.com and https://dev.to for updates"
        urls = _extract_all_urls(text)
        assert len(urls) == 2
        assert "https://github.com" in urls[0]

    def test_extract_url_strips_punctuation(self):
        from screenmind.workers.analysis_worker import _extract_all_urls
        urls = _extract_all_urls("See https://example.com/page.")
        assert urls[0] == "https://example.com/page"


class TestAnalysisWorkerBackfill:
    """Backfill re-analyzes rows that were never analyzed."""

    async def test_backfill_passes_detected_app_as_hint(self, db, tmp_path):
        """Unanalyzed rows have app_name NULL; the OS-detected app must be used."""
        from PIL import Image
        from screenmind.storage.models import ScreenshotEntry
        from screenmind.workers.analysis_worker import AnalysisWorker

        shot = tmp_path / "shot.jpg"
        Image.new("RGB", (64, 64), "white").save(shot)
        db.insert_activity(ScreenshotEntry(
            timestamp=datetime.now(),
            screenshot_path=str(shot),
            window_title="Chats",
            detected_app_name="Telegram",
            analyzed=False,
        ))

        worker = AnalysisWorker(queue=asyncio.Queue(maxsize=100), database=db)
        worker._process = AsyncMock()
        await worker._backfill_skipped()

        worker._process.assert_awaited_once()
        capture = worker._process.await_args.args[0]
        assert capture.app_name == "Telegram"
        assert capture.window_title == "Chats"


class TestEmptyScreenRule:
    """A frame with no app, no title and no text skips Gemma and is stored as idle."""

    def _setup(self, db, tmp_path, ocr_text, app=None, title=None):
        from PIL import Image
        from screenmind.storage.models import ScreenshotEntry
        from screenmind.workers.analysis_worker import AnalysisWorker

        shot = tmp_path / "shot.jpg"
        Image.new("RGB", (64, 64), "teal").save(shot)
        activity_id = db.insert_activity(ScreenshotEntry(
            timestamp=datetime.now(), screenshot_path=str(shot),
            window_title=title, detected_app_name=app, analyzed=False,
        ))
        worker = AnalysisWorker(queue=asyncio.Queue(maxsize=100), database=db)
        worker._ocr = MagicMock(is_available=True)
        worker._ocr.extract_text_with_boxes.return_value = (ocr_text, [])
        worker._analyzer = MagicMock()
        worker._analyzer.analyze_screenshot_fast.side_effect = RuntimeError("gemma called")
        worker._analyzer.analyze_screenshot_balanced.side_effect = RuntimeError("gemma called")
        worker._analyzer.analyze_screenshot.side_effect = RuntimeError("gemma called")
        capture = CaptureResult(
            filepath=shot, timestamp=datetime.now(), window_title=title, app_name=app,
            image=Image.open(shot), activity_id=activity_id,
        )
        return worker, capture, activity_id

    def _gemma_called(self, worker):
        a = worker._analyzer
        return (a.analyze_screenshot_fast.called or a.analyze_screenshot_balanced.called
                or a.analyze_screenshot.called)

    async def test_wallpaper_only_is_idle_without_gemma(self, db, tmp_path):
        worker, capture, activity_id = self._setup(db, tmp_path, ocr_text="")
        await worker._process(capture)

        row = db.get_activity_by_id(activity_id)
        assert row["category"] == "idle"
        assert row["analysis_method"] == "rule:empty_screen"
        assert not self._gemma_called(worker)

    async def test_unlabeled_display_with_text_still_analyzed(self, db, tmp_path):
        """A floating call window has no normal window but plenty of text."""
        worker, capture, _ = self._setup(
            db, tmp_path, ocr_text="Max Bigin  Daniil Golovin  Pavel Granin  Leave call")
        await worker._process(capture)
        assert self._gemma_called(worker)

    async def test_labeled_frame_never_hits_rule(self, db, tmp_path):
        worker, capture, _ = self._setup(
            db, tmp_path, ocr_text="", app="Google Chrome", title="New Tab")
        await worker._process(capture)
        assert self._gemma_called(worker)


class TestCaptureAllMonitors:
    """Several displays: one entry per display, each labeled with its own app."""

    LAPTOP = {"left": 0, "top": 0, "width": 1512, "height": 982}
    DELL = {"left": 1512, "top": 0, "width": 2288, "height": 1287}

    def _make_worker(self, tmp_path, tops, active, can_find=True):
        from PIL import Image

        worker = CaptureWorker(queue=asyncio.Queue(maxsize=100))
        colors = iter([(255, 0, 0), (0, 0, 255), (0, 255, 0), (255, 255, 0)])

        def capture(monitor=None):
            path = tmp_path / f"{monitor['left']}.jpg"
            img = Image.new("RGB", (64, 64), next(colors))
            # Make the frames visually distinct for pHash
            for x in range(0, 64, 8 if monitor is self.LAPTOP else 16):
                for y in range(64):
                    img.putpixel((x, y), (0, 0, 0))
            img.save(path)
            worker._screen._last_monitor_key = f"{monitor['left']},{monitor['top']}"
            return path, img

        screen = MagicMock()
        screen.monitors_to_capture.return_value = [self.LAPTOP, self.DELL]
        screen.active_monitor.return_value = active
        screen.capture.side_effect = capture
        type(screen).last_monitor_key = property(lambda s: s._last_monitor_key)
        worker._screen = screen
        worker._a11y = MagicMock(is_available=True)
        worker._a11y.extract_text.return_value = ("focused window text", "a11y")

        patches = [
            patch("screenmind.workers.capture_worker.get_active_app_name", return_value="Claude"),
            patch("screenmind.workers.capture_worker.get_active_window_title", return_value="Claude"),
            patch("screenmind.workers.capture_worker.get_top_window_in",
                  side_effect=lambda m: tops.get(m["left"])),
            patch("screenmind.workers.capture_worker.can_find_top_window", return_value=can_find),
        ]
        for p in patches:
            p.start()
        return worker, patches

    async def _drain(self, worker):
        items = []
        while not worker._queue.empty():
            items.append(await worker._queue.get())
        return items

    @pytest.mark.asyncio
    async def test_one_entry_per_display(self, tmp_path):
        tops = {0: ("Google Chrome", "Meet - Standup"), 1512: ("Claude", "Claude")}
        worker, patches = self._make_worker(tmp_path, tops, active=self.DELL)
        try:
            await worker._capture_tick()
            items = await self._drain(worker)
        finally:
            for p in patches:
                p.stop()

        by_app = {i.app_name: i for i in items}
        assert set(by_app) == {"Google Chrome", "Claude"}
        assert by_app["Google Chrome"].window_title == "Meet - Standup"
        # a11y reads the focused window, so only the focused display gets it
        assert by_app["Claude"].a11y_text == "focused window text"
        assert by_app["Google Chrome"].a11y_text is None

    @pytest.mark.asyncio
    async def test_blocked_app_display_skipped(self, tmp_path):
        tops = {0: ("1Password", "Vault"), 1512: ("Claude", "Claude")}
        worker, patches = self._make_worker(tmp_path, tops, active=self.DELL)
        try:
            with patch.object(type(settings_mod.settings), "blocked_apps_list",
                              new_callable=lambda: property(lambda s: ["1password"])):
                await worker._capture_tick()
            items = await self._drain(worker)
        finally:
            for p in patches:
                p.stop()

        assert [i.app_name for i in items] == ["Claude"]

    @pytest.mark.asyncio
    async def test_unknown_display_app_without_adapter_support(self, tmp_path):
        """Windows/Linux: no per-display lookup, so only the focused display is labeled."""
        worker, patches = self._make_worker(tmp_path, {}, active=self.DELL, can_find=False)
        try:
            await worker._capture_tick()
            items = await self._drain(worker)
        finally:
            for p in patches:
                p.stop()

        by_path = {i.filepath.name: i for i in items}
        assert by_path["1512.jpg"].app_name == "Claude"
        assert by_path["0.jpg"].app_name is None

    @pytest.mark.asyncio
    async def test_empty_display_not_labeled_with_focused_app(self, tmp_path):
        """macOS: a wallpaper-only display must not borrow the focused app's
        name or accessibility text, even when it counts as the focused display
        (the focus lookup falls back to the primary display)."""
        tops = {1512: ("Google Chrome", "Device-based workflow capture")}
        worker, patches = self._make_worker(tmp_path, tops, active=self.LAPTOP)
        try:
            await worker._capture_tick()
            items = await self._drain(worker)
        finally:
            for p in patches:
                p.stop()

        by_path = {i.filepath.name: i for i in items}
        empty = by_path["0.jpg"]
        assert empty.app_name is None
        assert empty.window_title is None
        assert empty.a11y_text is None
        assert by_path["1512.jpg"].app_name == "Google Chrome"

    @pytest.mark.asyncio
    async def test_dedup_is_per_display(self, tmp_path):
        """Alternating displays must not reset each other's dedup hash."""
        tops = {0: ("Google Chrome", "Meet"), 1512: ("Claude", "Claude")}
        worker, patches = self._make_worker(tmp_path, tops, active=self.DELL)
        try:
            await worker._capture_tick()
            first = await self._drain(worker)
            worker._screen.capture.side_effect = None
            frames = {i.filepath.name: i.image for i in first}
            worker._screen.capture.side_effect = lambda monitor=None: (
                tmp_path / f"{monitor['left']}.jpg", frames[f"{monitor['left']}.jpg"]
            )
            await worker._capture_tick()
            second = await self._drain(worker)
        finally:
            for p in patches:
                p.stop()

        assert len(first) == 2
        assert second == []
        assert worker._consecutive_skips == 1


class TestBacklogSkip:
    """Frames too old to analyze keep what was read at capture time."""

    def _stale_capture(self, db, tmp_path, **kw):
        from datetime import timedelta
        from PIL import Image
        from screenmind.storage.models import ScreenshotEntry

        shot = tmp_path / "shot.jpg"
        Image.new("RGB", (64, 64), "white").save(shot)
        ts = datetime.now() - timedelta(seconds=600)
        activity_id = db.insert_activity(ScreenshotEntry(
            timestamp=ts, screenshot_path=str(shot), window_title="Twitch — Mozilla Firefox",
            detected_app_name="firefox", analyzed=False,
        ))
        db.set_user_actions(activity_id, "- clicked 'Browse'")
        return CaptureResult(
            filepath=shot, timestamp=ts, window_title="Twitch — Mozilla Firefox",
            app_name="firefox", image=Image.open(shot), activity_id=activity_id,
            user_actions="- clicked 'Browse'", **kw,
        )

    async def _run_once(self, db, capture):
        from screenmind.workers.analysis_worker import AnalysisWorker

        queue = asyncio.Queue(maxsize=10)
        worker = AnalysisWorker(queue=queue, database=db)
        worker._backfill_skipped = AsyncMock()
        worker._ocr = MagicMock(is_available=True)
        worker._analyzer = MagicMock()
        await queue.put(capture)
        task = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(queue.join(), timeout=5)
        finally:
            worker.stop()
            task.cancel()
        return worker

    async def test_skip_keeps_capture_data(self, db, tmp_path):
        capture = self._stale_capture(
            db, tmp_path, browser_url="https://www.twitch.tv/",
            a11y_text="Browse channels\nSSN 123-45-6789",
        )
        worker = await self._run_once(db, capture)

        row = db.get_activity_by_id(capture.activity_id)
        assert row["status"] == "skipped"
        assert row["analysis_method"] == "skipped"
        assert row["active_url"] == "https://www.twitch.tv/"
        assert "Browse channels" in row["ocr_text"]
        assert "123-45-6789" not in row["ocr_text"]  # same filter as the normal path
        assert row["user_actions"] == "- clicked 'Browse'"
        assert row["app_name"] == "firefox"
        # Nothing analyzed the frame: no made-up category or summary
        assert row["category"] is None
        assert row["summary"] is None
        assert not worker._ocr.extract_text_with_boxes.called
        assert not worker._analyzer.method_calls

    async def test_skipped_frames_not_in_analytics(self, db, tmp_path):
        capture = self._stale_capture(db, tmp_path, a11y_text="text")
        await self._run_once(db, capture)

        day = capture.timestamp.date().isoformat()
        stats = db.get_stats(day, day)
        assert stats["category_breakdown"] == {}
        assert stats["total_activities"] == 0
        assert stats["status_breakdown"] == {"skipped": 1}

    async def test_backfill_reuses_kept_text_and_url(self, db, tmp_path):
        from screenmind.workers.analysis_worker import AnalysisWorker

        capture = self._stale_capture(
            db, tmp_path, browser_url="https://www.twitch.tv/", a11y_text="Browse channels")
        db.mark_skipped(capture.activity_id, ocr_text="Browse channels",
                        active_url="https://www.twitch.tv/")
        db._get_conn().execute("UPDATE activities SET timestamp = ? WHERE id = ?",
                               (datetime.now().isoformat(), capture.activity_id))
        db._get_conn().commit()

        worker = AnalysisWorker(queue=asyncio.Queue(maxsize=10), database=db)
        worker._process = AsyncMock()
        await worker._backfill_skipped()

        again = worker._process.await_args.args[0]
        assert again.a11y_text == "Browse channels"
        assert again.browser_url == "https://www.twitch.tv/"
        assert again.user_actions == "- clicked 'Browse'"


class TestFocusConsistency:
    """A11y text and URL must describe the same window as the image and title."""

    def _make_worker(self, tmp_path, titles, db=None):
        from PIL import Image

        worker = CaptureWorker(queue=asyncio.Queue(maxsize=10), database=db)
        shot = tmp_path / "shot.jpg"
        Image.new("RGB", (64, 64), "white").save(shot)
        screen = MagicMock()
        screen.monitors_to_capture.return_value = [None]
        screen.capture.return_value = (shot, Image.open(shot))
        screen.last_monitor_key = None
        worker._screen = screen
        worker._a11y = MagicMock(is_available=True)
        calls = []
        worker._a11y.extract_text.side_effect = lambda: (calls.append("a11y"), ("page text", "a11y"))[1]

        title_iter = iter(titles)
        patches = [
            patch("screenmind.workers.capture_worker.get_active_app_name", return_value="chrome"),
            patch("screenmind.workers.capture_worker.get_active_window_title",
                  side_effect=lambda: next(title_iter)),
            patch("screenmind.workers.capture_worker._get_browser_url",
                  side_effect=lambda: (calls.append("url"), "https://ru.wikipedia.org/wiki/GitHub")[1]),
        ]
        for p in patches:
            p.start()
        return worker, patches, calls

    async def _tick(self, worker, patches):
        try:
            await worker._capture_tick()
        finally:
            for p in patches:
                p.stop()
        return await worker._queue.get()

    async def test_same_window_keeps_text_and_url(self, tmp_path):
        worker, patches, _ = self._make_worker(tmp_path, ["GitHub — Wikipedia", "GitHub — Wikipedia"])
        item = await self._tick(worker, patches)
        assert item.browser_url == "https://ru.wikipedia.org/wiki/GitHub"
        assert item.a11y_text == "page text"

    async def test_tab_switch_mid_capture_drops_text_and_url(self, tmp_path):
        """#184: the dashboard's title with the URL of the tab clicked next."""
        worker, patches, _ = self._make_worker(
            tmp_path, ["ScreenMind — Dashboard", "GitHub — Wikipedia"])
        item = await self._tick(worker, patches)
        assert item.window_title == "ScreenMind — Dashboard"
        assert item.browser_url is None
        assert item.a11y_text is None

    async def test_read_before_db_insert_and_event_linking(self, db, tmp_path):
        worker, patches, calls = self._make_worker(tmp_path, ["T", "T"], db=db)
        db_insert = db.insert_activity
        db.insert_activity = lambda entry: (calls.append("insert"), db_insert(entry))[1]

        async def link(activity_id, now):
            calls.append("link")
            return None
        worker._link_ui_events = link

        await self._tick(worker, patches)
        assert calls == ["url", "a11y", "insert", "link"]

    async def test_window_title_filtered_before_storage(self, db, tmp_path):
        title = "notes — password: Tr0ub4dor&3 - Notepad"
        worker, patches, _ = self._make_worker(tmp_path, [title, title], db=db)
        item = await self._tick(worker, patches)

        assert "Tr0ub4dor&3" not in item.window_title
        assert "REDACTED" in item.window_title
        assert db.get_activity_by_id(item.activity_id)["window_title"] == item.window_title
        # The raw titles still match, so the focus check keeps the reads
        assert item.a11y_text == "page text"


class TestUiEventLinkGuard:
    """A hung UI-event recorder must not stop capture."""

    async def test_stuck_link_times_out_and_is_skipped_until_it_returns(self, monkeypatch):
        import threading
        import screenmind.workers.capture_worker as cw

        monkeypatch.setattr(cw, "LINK_UI_EVENTS_TIMEOUT_S", 0.2)
        monkeypatch.setattr(settings_mod.settings, "ui_events_enabled", True)
        release = threading.Event()
        flushes = []

        def flush():
            flushes.append(1)
            release.wait(5)

        worker = CaptureWorker(queue=asyncio.Queue(maxsize=10), database=MagicMock())
        worker._db.attach_ui_events.return_value = False
        worker._ui_recorder = MagicMock()
        worker._ui_recorder.flush_for_capture.side_effect = flush

        assert await worker._link_ui_events(1, datetime.now()) is None
        stuck = worker._stuck_link
        assert stuck is not None and not stuck.done()

        # Still stuck: the next frame doesn't wait or pile up another call
        assert await asyncio.wait_for(worker._link_ui_events(2, datetime.now()), 0.1) is None
        assert len(flushes) == 1

        release.set()
        await stuck
        await worker._link_ui_events(3, datetime.now())
        assert len(flushes) == 2
        assert worker._stuck_link is None


class TestOcrBoxRedaction:
    """Stored OCR boxes and organized_text get the same filter as ocr_text."""

    async def test_secrets_redacted_in_boxes_and_organized_text(self, db, tmp_path, monkeypatch):
        import json
        from PIL import Image
        from screenmind.storage.models import ActivityRecord, ScreenshotEntry
        from screenmind.workers.analysis_worker import AnalysisWorker

        monkeypatch.setattr(settings_mod.settings, "analysis_mode", "fast")
        shot = tmp_path / "shot.jpg"
        Image.new("RGB", (400, 200), "white").save(shot)
        activity_id = db.insert_activity(ScreenshotEntry(
            timestamp=datetime.now(), screenshot_path=str(shot), window_title="notes.txt - Notepad",
            detected_app_name="notepad", analyzed=False,
        ))

        def box(x, y, text):
            return {"box": [[x, y], [x + 120, y], [x + 120, y + 20], [x, y + 20]], "text": text, "conf": 0.9}
        boxes = [
            box(10, 10, "Server login notes"),
            box(10, 40, "pwd"), box(140, 40, "- Tr0ub4dor&3"),  # label and value split by OCR
            box(10, 70, "password: hunter2x9"),
        ]
        raw = "\n".join(b["text"] for b in boxes)

        worker = AnalysisWorker(queue=asyncio.Queue(maxsize=10), database=db)
        worker._ocr = MagicMock(is_available=True)
        worker._ocr.extract_text_with_boxes.return_value = (raw, boxes)
        worker._analyzer = MagicMock()
        worker._analyzer.analyze_screenshot_fast.return_value = (
            ActivityRecord(app_name="Notepad", activity_category="writing",
                           activity_summary="Editing notes", scene_description="A text file"),
            [],
        )
        capture = CaptureResult(
            filepath=shot, timestamp=datetime.now(), window_title="notes.txt - Notepad",
            app_name="notepad", image=Image.open(shot), activity_id=activity_id,
        )
        await worker._process(capture)

        row = db.get_activity_by_id(activity_id)
        stored_boxes = row["ocr_boxes"] if isinstance(row["ocr_boxes"], str) else json.dumps(row["ocr_boxes"])
        assert row["organized_text"]
        for secret in ("Tr0ub4dor&3", "hunter2x9"):
            assert secret not in stored_boxes
            assert secret not in row["organized_text"]
            assert secret not in row["ocr_text"]
            assert secret not in worker._analyzer.analyze_screenshot_fast.call_args.kwargs["ocr_text"]
        assert "Server login notes" in row["organized_text"]
