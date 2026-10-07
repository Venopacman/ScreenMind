"""Data export routes: a zip of sessions per day for one user."""

import os
import tempfile
from datetime import date
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

import screenmind.api.dependencies as deps
from screenmind.export.archive import MAX_DAYS, archive_name, summarize, write_export
from screenmind.export.sessions import IdleGapSplitter

router = APIRouter(prefix="/api/export", tags=["export"])

_LOCAL_HOSTS = ("127.0.0.1", "::1")


def _check_local(request: Request):
    # The server binds to 127.0.0.1 by default. The archive holds screenshots,
    # so refuse it even if someone binds the API to a wider address.
    host = request.client.host if request.client else None
    if host not in _LOCAL_HOSTS:
        raise HTTPException(status_code=403, detail="Export works only from this computer.")


def _parse_range(day: Optional[str], date_from: Optional[str], date_to: Optional[str]):
    try:
        if day:
            start = end = date.fromisoformat(day)
        elif date_from:
            start = date.fromisoformat(date_from)
            end = date.fromisoformat(date_to) if date_to else start
        else:
            raise HTTPException(status_code=400, detail="Pass date, or from and to (YYYY-MM-DD).")
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD.")
    if end < start:
        raise HTTPException(status_code=400, detail="'to' is before 'from'.")
    if (end - start).days + 1 > MAX_DAYS:
        raise HTTPException(status_code=400, detail=f"Export at most {MAX_DAYS} days at a time.")
    return start, end


@router.get("/preview")
async def export_preview(
    request: Request,
    day: Optional[str] = Query(None, alias="date"),
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    gap_minutes: float = Query(10, gt=0, le=24 * 60),
):
    """Counts and screenshot size per day, so the UI can show them before download."""
    _check_local(request)
    start, end = _parse_range(day, date_from, date_to)
    return await run_in_threadpool(summarize, deps.db, start, end, IdleGapSplitter(gap_minutes))


@router.get("")
async def export_zip(
    request: Request,
    user: str = Query(..., min_length=1, max_length=200, description="User id or email to put in the archive"),
    day: Optional[str] = Query(None, alias="date"),
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    screenshots: bool = Query(True),
    gap_minutes: float = Query(10, gt=0, le=24 * 60),
):
    """Download a zip: one folder per day, one text file per session, plus screenshots."""
    _check_local(request)
    user = user.strip()
    if not user:
        raise HTTPException(status_code=400, detail="Type a user id or email.")
    start, end = _parse_range(day, date_from, date_to)

    fd, path = tempfile.mkstemp(prefix="screenmind-export-", suffix=".zip")
    try:
        with os.fdopen(fd, "wb") as f:
            await run_in_threadpool(
                write_export, deps.db, f, user, start, end,
                IdleGapSplitter(gap_minutes), screenshots,
            )
    except Exception:
        os.unlink(path)
        raise
    return FileResponse(
        path,
        media_type="application/zip",
        filename=archive_name(user, start, end) + ".zip",
        background=BackgroundTask(os.unlink, path),
    )
