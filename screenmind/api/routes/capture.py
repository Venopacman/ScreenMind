"""Capture control routes — pause, resume, incognito, status."""

from fastapi import APIRouter

from screenmind.api.dependencies import capture_worker, analysis_worker
from screenmind.engine import model_manager

router = APIRouter(prefix="/api", tags=["capture"])


@router.post("/capture/pause")
async def pause_capture():
    if capture_worker:
        capture_worker.pause(source="dashboard")
    return {"paused": True}


@router.post("/capture/resume")
async def resume_capture():
    if capture_worker:
        capture_worker.resume(source="dashboard")
    return {"paused": False}


@router.post("/incognito/toggle")
async def toggle_incognito():
    """Toggle incognito mode (pause capture with no trace)."""
    if capture_worker:
        if getattr(capture_worker, 'incognito', False):
            capture_worker.incognito = False
            capture_worker.resume(source="incognito_off")
            return {"incognito": False}
        else:
            capture_worker.incognito = True
            capture_worker.pause(source="incognito_on")
            return {"incognito": True}
    return {"error": "Capture worker not available"}


@router.get("/status")
async def get_status():
    """System status for the dashboard, including model/server state."""
    return {
        "status": "running",
        "capture": capture_worker.stats if capture_worker else {},
        "analysis": analysis_worker.stats if analysis_worker else {},
        "incognito": getattr(capture_worker, 'incognito', False) if capture_worker else False,
        "model": model_manager.get_model_status(),
    }

