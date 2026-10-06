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

    def test_trigger_bookmark(self):
        worker, _ = self._make_worker()
        assert worker._pending_bookmark is False
        worker.trigger_bookmark()
        assert worker._pending_bookmark is True

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
        assert result.bookmarked is False
        assert result.activity_id is None
        assert result.a11y_text is None
        assert result.phash is None

    def test_create_bookmarked(self, tmp_path):
        result = CaptureResult(
            filepath=tmp_path / "test.jpg",
            timestamp=datetime.now(),
            bookmarked=True,
        )
        assert result.bookmarked is True


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
        assert len(worker._priority_items) == 0

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
            bookmarked=False,
            analyzed=False,
        ))

        worker = AnalysisWorker(queue=asyncio.Queue(maxsize=100), database=db)
        worker._process = AsyncMock()
        await worker._backfill_skipped()

        worker._process.assert_awaited_once()
        capture = worker._process.await_args.args[0]
        assert capture.app_name == "Telegram"
        assert capture.window_title == "Chats"


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
