"""Data management routes — clear timeline, delete old data."""

from fastapi import APIRouter, Query

from evolve_capture.api.dependencies import db, analysis_worker

router = APIRouter(prefix="/api", tags=["data"])


@router.delete("/timeline/clear")
async def clear_timeline(date: str = Query(default=None)):
    """Delete all activities for a specific date."""
    target_date = date or str(__import__("datetime").date.today())
    deleted = db.delete_by_date(target_date)
    # Flush analysis queue to prevent stale items from blocking new captures
    if analysis_worker:
        analysis_worker.flush_queue()
    return {"deleted": deleted, "date": target_date}


@router.delete("/activities/before/{date}")
async def delete_old(date: str):
    count = db.delete_before(date)
    return {"deleted": count, "before_date": date}
