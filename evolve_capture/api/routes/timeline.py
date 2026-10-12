"""Timeline routes — list activities, activity detail, delete."""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from evolve_capture.api.dependencies import db

router = APIRouter(prefix="/api", tags=["timeline"])

@router.get("/timeline")
async def get_timeline(
    date: str = Query(default=None),
    since: str = Query(default=None, description="ISO timestamp cutoff — only return activities after this time"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """Get activities for a specific date, optionally filtered by since timestamp."""
    target_date = date or str(__import__("datetime").date.today())
    activities = db.get_activities_by_date(target_date, limit=limit, offset=offset)
    if since:
        activities = [a for a in activities if a.get("timestamp", "") >= since]
    for a in activities:
        if a.get("screenshot_path"):
            a["screenshot_url"] = f"/api/screenshot/{a['id']}"
    return {"date": target_date, "activities": activities}


@router.get("/activity/{activity_id}")
async def get_activity(activity_id: int):
    """Get a single activity with full details."""
    activity = db.get_activity_by_id(activity_id)
    if not activity:
        raise HTTPException(status_code=404, detail="Activity not found")
    if activity.get("screenshot_path"):
        activity["screenshot_url"] = f"/api/screenshot/{activity['id']}"
    activity["ui_events"] = db.get_ui_events(activity_id)
    return activity


@router.delete("/activities/{activity_id}")
async def delete_activity(activity_id: int):
    """Delete a single activity and its screenshot."""
    conn = db._get_conn()
    row = conn.execute(
        "SELECT screenshot_path FROM activities WHERE id = ?", (activity_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Activity not found")

    # Delete screenshot file
    ss_path = row[0] if row else None
    if ss_path and Path(ss_path).exists():
        try:
            Path(ss_path).unlink()
        except OSError:
            pass

    # Delete from DB
    conn.execute("DELETE FROM activities WHERE id = ?", (activity_id,))
    conn.commit()
    return {"deleted": True, "activity_id": activity_id}
