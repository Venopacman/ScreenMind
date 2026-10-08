"""Meeting routes — tracked calls (and transcripts of older calls), CRUD."""

import logging

from fastapi import APIRouter, HTTPException, Query

from screenmind.api.dependencies import db, audio_worker

logger = logging.getLogger("screenmind.api.routes.meetings")

router = APIRouter(prefix="/api/meetings", tags=["meetings"])


@router.get("")
async def get_meetings(date: str = Query(default=None)):
    """Get the calls of a date."""
    target_date = date or str(__import__("datetime").date.today())
    meetings = db.get_meetings_by_date(target_date)
    return {"meetings": meetings, "date": target_date}


# NOTE: /status must be defined BEFORE /{meeting_id} so FastAPI
# doesn't try to parse "status" as an integer meeting_id.
@router.get("/status")
async def meeting_status():
    """Get the current call tracking status."""
    if audio_worker:
        return audio_worker.stats
    return {"tracking": False, "in_meeting": False}


@router.get("/{meeting_id}")
async def get_meeting(meeting_id: int):
    """Get a single meeting by ID."""
    meeting = db.get_meeting_by_id(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting


@router.delete("/{meeting_id}")
async def delete_meeting(meeting_id: int):
    """Delete a meeting by ID. Ends the call first if it is the current one."""
    # If this is the current call, end it first
    if audio_worker and audio_worker.in_meeting and audio_worker._meeting_id == meeting_id:
        try:
            audio_worker.force_stop()
            logger.warning(f"Ended the current call to delete meeting {meeting_id}")
        except Exception as e:
            logger.error(f"Force-stop failed: {e}")

    deleted = db.delete_meeting(meeting_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return {"status": "deleted", "id": meeting_id}

