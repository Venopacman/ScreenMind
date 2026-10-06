"""UI event routes — recorder status, permission prompts, event queries."""

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Query

from screenmind.api import dependencies
from screenmind.capture.ui_events.models import ALL_EVENT_TYPES, describe_event
from screenmind.config import settings

router = APIRouter(prefix="/api/ui-events", tags=["ui-events"])


@router.get("/status")
async def ui_events_status():
    """Whether UI event recording is supported, enabled, running and permitted."""
    recorder = dependencies.ui_recorder
    if recorder is None:
        return {
            "supported": False, "enabled": settings.ui_events_enabled, "running": False,
            "permissions": None, "events_recorded": 0, "last_error": None,
            "types": ALL_EVENT_TYPES,
        }
    return {**recorder.status(), "types": ALL_EVENT_TYPES}


@router.post("/permissions")
async def ui_events_request_permissions():
    """Show the OS permission prompts (macOS: Input Monitoring, Accessibility)."""
    recorder = dependencies.ui_recorder
    if recorder is None or not recorder.supported:
        return {"ok": False, "error": "UI events are not supported on this platform"}
    perms = recorder.request_permissions()
    # A grant only takes effect for a hook installed after it, so restart.
    if perms and perms["all_granted"] and recorder.running:
        recorder.stop()
        recorder.sync_with_settings()
    return {"ok": True, "permissions": perms}


@router.get("")
async def list_ui_events(
    start: Optional[str] = Query(default=None, description="ISO timestamp, default: 1 hour ago"),
    end: Optional[str] = Query(default=None, description="ISO timestamp, default: now"),
    type: Optional[str] = Query(default=None, description="Filter by event type"),
    limit: int = Query(default=200, ge=1, le=2000),
):
    """UI events in a time range, oldest first, each with a readable description."""
    now = datetime.now()
    start = start or (now - timedelta(hours=1)).isoformat()
    end = end or now.isoformat()
    events = dependencies.db.get_ui_events_range(start, end, event_type=type, limit=limit)
    for e in events:
        e["description"] = describe_event(
            e["type"], e["app_name"], e["window_title"],
            e["element_role"], e["element_name"], e["text"],
        )
    return {"start": start, "end": end, "events": events}
