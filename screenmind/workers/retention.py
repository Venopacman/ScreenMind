"""
Retention: delete data older than settings.retention_days.

It runs at startup and then again while the app runs (G24). The main
instance can run for weeks, and a startup-only cleanup deleted nothing in
that time. The cutoff is a date, so a second run on the same day with the
same setting deletes nothing new. run() checks once an hour and cleans up
when the date or retention_days has changed since the last run, so a change
in Settings applies within the hour.
"""

import asyncio
import logging
from datetime import date

from screenmind.config import settings

logger = logging.getLogger("screenmind.workers.retention")

CHECK_INTERVAL_S = 3600


class Retention:
    def __init__(self, database, today=date.today):
        self._db = database
        self._today = today
        self._last: tuple[date, int] | None = None  # (date, retention_days) of the last run

    def due(self) -> int | None:
        """The retention_days to clean up with now, or None if there is nothing new."""
        key = (self._today(), settings.retention_days)
        if key == self._last:
            return None
        self._last = key
        return key[1] if key[1] > 0 else None

    def cleanup(self, days: int):
        cleaned = self._db.cleanup_old_data(days)
        if cleaned["activities"] > 0 or cleaned["meetings"] > 0:
            logger.info(f"Retention cleanup: removed {cleaned['activities']} activities, "
                        f"{cleaned['meetings']} meetings older than {days} days")

    def run_if_due(self):
        """Clean up now if due. Blocks; main() calls it once at startup."""
        days = self.due()
        if days is not None:
            self.cleanup(days)

    async def run(self, interval_s: float = CHECK_INTERVAL_S):
        """Check every interval_s until cancelled. The cleanup deletes files,
        so it runs in a thread. A stop during a cleanup waits for it, so no
        screenshot is left on disk without its row."""
        while True:
            await asyncio.sleep(interval_s)
            days = self.due()
            if days is None:
                continue
            try:
                await asyncio.to_thread(self.cleanup, days)
            except Exception:
                logger.exception("Retention cleanup failed")
