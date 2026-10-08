"""A stop must not wait for a Gemma call in flight.

asyncio.run() and the interpreter wait for executor threads at exit, so
analysis runs on a DaemonExecutor that a stop abandons. The frame whose call
was cut off stays 'pending' for backfill.
"""
import asyncio
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from screenmind.workers.daemon_executor import DaemonExecutor

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestDaemonExecutor:

    def test_runs_jobs_in_order_on_one_daemon_thread(self):
        ex = DaemonExecutor("t-order")
        seen = []
        futures = [ex.submit(lambda i=i: seen.append((i, threading.current_thread()))) for i in range(3)]
        for f in futures:
            f.result(timeout=5)
        assert [i for i, _ in seen] == [0, 1, 2]
        threads = {t for _, t in seen}
        assert len(threads) == 1
        (thread,) = threads
        assert thread.daemon and thread.name == "t-order"
        ex.shutdown()

    def test_result_and_exception(self):
        ex = DaemonExecutor("t-result")
        assert ex.submit(lambda a, b=0: a + b, 2, b=3).result(timeout=5) == 5
        with pytest.raises(ValueError):
            ex.submit(lambda: (_ for _ in ()).throw(ValueError("x"))).result(timeout=5)
        ex.shutdown()

    def test_shutdown_without_wait_abandons_the_running_job(self):
        ex = DaemonExecutor("t-abandon")
        release = threading.Event()
        started = threading.Event()

        def block():
            started.set()
            release.wait(30)
            return "late"

        running = ex.submit(block)
        queued = ex.submit(lambda: "never")
        assert started.wait(5)

        t0 = time.monotonic()
        ex.shutdown(wait=False, cancel_futures=True)
        assert time.monotonic() - t0 < 1
        assert queued.cancelled()
        assert not running.done()
        with pytest.raises(RuntimeError):
            ex.submit(lambda: None)
        release.set()
        assert running.result(timeout=5) == "late"

    def test_process_exit_does_not_wait_for_the_job(self):
        """The real case: asyncio.run() ends while a call still runs."""
        code = textwrap.dedent("""
            import asyncio, time
            from screenmind.workers.daemon_executor import DaemonExecutor
            ex = DaemonExecutor("analysis")
            async def main():
                loop = asyncio.get_running_loop()
                task = asyncio.ensure_future(loop.run_in_executor(ex, time.sleep, 60))
                await asyncio.sleep(0.3)
                task.cancel()
                ex.shutdown(wait=False, cancel_futures=True)
            asyncio.run(main())
        """)
        t0 = time.monotonic()
        result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                                capture_output=True, text=True, timeout=50)
        elapsed = time.monotonic() - t0
        assert result.returncode == 0, result.stderr
        assert elapsed < 20


class TestStopDuringAnalysis:

    async def test_stop_abandons_the_call_and_the_frame_stays_pending(self, db, tmp_path, monkeypatch):
        from PIL import Image
        from screenmind.config import settings
        from screenmind.storage.models import ScreenshotEntry
        from screenmind.workers.analysis_worker import AnalysisWorker
        from screenmind.workers.capture_worker import CaptureResult

        monkeypatch.setattr(settings, "defer_analysis", False)
        shot = tmp_path / "shot.jpg"
        Image.new("RGB", (64, 64), "teal").save(shot)
        activity_id = db.insert_activity(ScreenshotEntry(
            timestamp=datetime.now(), screenshot_path=str(shot),
            window_title="Inbox", detected_app_name="Mail", analyzed=False,
        ))
        queue: asyncio.Queue = asyncio.Queue(maxsize=10)
        worker = AnalysisWorker(queue=queue, database=db)
        worker._ocr = MagicMock(is_available=True)
        worker._ocr.extract_text_with_boxes.return_value = ("Inbox: 3 unread messages", [])

        in_call = threading.Event()
        release = threading.Event()
        call_threads = []

        def slow_gemma(**_kwargs):
            call_threads.append(threading.current_thread())
            in_call.set()
            release.wait(30)
            raise ConnectionError("llama-server went away")  # what a stop used to cause

        worker._analyzer = MagicMock()
        worker._analyzer.analyze_screenshot_fast.side_effect = slow_gemma
        worker._analyzer.analyze_screenshot_balanced.side_effect = slow_gemma
        worker._analyzer.analyze_screenshot.side_effect = slow_gemma

        await queue.put(CaptureResult(
            filepath=shot, timestamp=datetime.now(), window_title="Inbox", app_name="Mail",
            image=Image.open(shot), activity_id=activity_id, phash=1,
        ))
        task = asyncio.create_task(worker.run())
        try:
            for _ in range(100):
                if in_call.is_set():
                    break
                await asyncio.sleep(0.05)
            assert in_call.is_set(), "the Gemma call never started"
            # Not the default executor: the exit joins its threads
            assert call_threads[0].daemon

            # What main() does on /api/shutdown
            t0 = time.monotonic()
            worker.stop()
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
            assert time.monotonic() - t0 < 2

            with pytest.raises(RuntimeError):
                worker._executor.submit(lambda: None)
        finally:
            release.set()

        # The call ends with an error after the stop; nothing may store it
        await asyncio.sleep(0.2)
        row = db.get_activity_by_id(activity_id)
        assert row["status"] == "pending"
        assert row["summary"] is None
