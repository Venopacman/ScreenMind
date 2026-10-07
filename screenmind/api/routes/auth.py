"""First-run flag for the welcome screen."""

import json

from fastapi import APIRouter

from screenmind.config import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/status")
async def auth_status():
    """Tell the dashboard whether to show the welcome screen."""
    # If settings.json exists with any data, the user already ran setup
    # (even without the flag).
    setup_complete = False
    try:
        if settings.settings_json_path.exists():
            data = json.loads(settings.settings_json_path.read_text())
            setup_complete = data.get("setup_complete", bool(data))
    except Exception:
        pass
    return {"first_run": not setup_complete}


@router.post("/setup-complete")
async def mark_setup_complete():
    """Mark first-run setup as complete (called after the welcome screen)."""
    settings.save_runtime_overrides({"setup_complete": True})
    return {"ok": True}
