"""Stats routes — analytics, heatmap, disk usage, storage estimate."""

from datetime import date, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Query

from screenmind.api.dependencies import db

router = APIRouter(prefix="/api", tags=["stats"])

# How many days each range covers, counting today.
RANGE_DAYS = {"day": 1, "week": 7, "month": 30}


@router.get("/stats")
async def get_stats(
    range_: Literal["day", "week", "month"] = Query(default="day", alias="range"),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
):
    """Get aggregated statistics.

    `range` picks a span ending today. Explicit `date_from`/`date_to` win over it.
    """
    today = date.today()
    df = date_from or str(today - timedelta(days=RANGE_DAYS[range_] - 1))
    dt = date_to or str(today)
    stats = db.get_stats(df, dt)
    return stats


@router.get("/stats/heatmap")
async def get_heatmap(
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
):
    today = str(date.today())
    df = date_from or str(date.today() - timedelta(days=7))
    dt = date_to or today
    return db.get_hourly_heatmap(df, dt)


@router.get("/disk")
async def get_disk_usage():
    return db.get_disk_usage()


@router.get("/storage-estimate")
async def storage_estimate():
    """Get storage usage estimates for different retention periods."""
    return db.get_storage_estimate()
