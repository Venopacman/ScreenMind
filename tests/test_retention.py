"""Retention runs at startup and again while the app runs (G24)."""
import asyncio
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

from screenmind.config import settings
from screenmind.storage.models import ScreenshotEntry
from screenmind.workers.retention import Retention

NOTHING = {"activities": 0, "meetings": 0}


class TestRunIfDue:

    def _retention(self, today):
        db = MagicMock()
        db.cleanup_old_data.return_value = NOTHING
        return db, Retention(db, today=lambda: today[0])

    def test_runs_once_per_day(self, monkeypatch):
        monkeypatch.setattr(settings, "retention_days", 7)
        today = [date(2026, 10, 8)]
        db, retention = self._retention(today)

        retention.run_if_due()
        retention.run_if_due()
        assert db.cleanup_old_data.call_count == 1

        today[0] = date(2026, 10, 9)
        retention.run_if_due()
        assert db.cleanup_old_data.call_count == 2

    def test_setting_change_runs_again_the_same_day(self, monkeypatch):
        monkeypatch.setattr(settings, "retention_days", 30)
        db, retention = self._retention([date(2026, 10, 8)])
        retention.run_if_due()

        monkeypatch.setattr(settings, "retention_days", 7)  # changed in Settings
        retention.run_if_due()
        assert [c.args[0] for c in db.cleanup_old_data.call_args_list] == [30, 7]

    def test_keep_forever_deletes_nothing(self, monkeypatch):
        monkeypatch.setattr(settings, "retention_days", 0)
        db, retention = self._retention([date(2026, 10, 8)])
        retention.run_if_due()
        db.cleanup_old_data.assert_not_called()


async def test_cleanup_runs_again_without_a_restart(db, monkeypatch):
    monkeypatch.setattr(settings, "retention_days", 7)
    today = [date.today()]
    retention = Retention(db, today=lambda: today[0])
    retention.run_if_due()  # the startup run

    # A frame that became older than the retention while the app ran
    old_id = db.insert_activity(ScreenshotEntry(
        timestamp=datetime.now() - timedelta(days=30), screenshot_path="x"))
    new_id = db.insert_activity(ScreenshotEntry(timestamp=datetime.now(), screenshot_path="y"))

    task = asyncio.create_task(retention.run(interval_s=0.05))
    try:
        await asyncio.sleep(0.2)
        assert db.get_activity_by_id(old_id) is not None  # same day: nothing due

        today[0] += timedelta(days=1)  # the next day
        for _ in range(40):
            if db.get_activity_by_id(old_id) is None:
                break
            await asyncio.sleep(0.05)
        assert db.get_activity_by_id(old_id) is None
        assert db.get_activity_by_id(new_id) is not None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
