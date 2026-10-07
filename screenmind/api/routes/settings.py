"""Settings routes — get/update config, startup registration, shutdown."""

import sys

from fastapi import APIRouter, Request

from screenmind.config import settings

router = APIRouter(prefix="/api", tags=["settings"])


# Keys the dashboard reads and writes
_DASHBOARD_KEYS = (
    "capture_interval", "performance_mode", "context_window", "kv_cache_quant",
    "flash_attention", "analysis_mode", "auto_pause_heavy_apps", "heavy_apps",
    "defer_analysis", "meeting_transcription", "meeting_apps", "retention_days",
    "sensitive_filter_enabled", "sensitive_filter_types", "encryption_enabled",
    "capture_active_monitor", "ui_events_enabled", "ui_events_types",
    "event_triggered_capture",
)


def _current() -> dict:
    return {k: getattr(settings, k) for k in _DASHBOARD_KEYS}


@router.get("/settings")
async def get_settings():
    """Return the settings shown on the dashboard."""
    return _current()


@router.post("/settings")
async def update_settings(request: Request):
    """Update settings (persists to settings.json)."""
    body = await request.json()
    settings.save_runtime_overrides(body)
    if "ui_events_enabled" in body:
        from screenmind.api import dependencies
        if dependencies.ui_recorder is not None:
            dependencies.ui_recorder.sync_with_settings()
    return {"status": "saved", **_current()}


@router.get("/startup/status")
async def get_startup_status():
    """Check if ScreenMind is registered in system startup."""
    from screenmind.startup import is_startup_installed
    return {"installed": is_startup_installed()}


@router.post("/startup/install")
async def install_startup_route():
    """Register ScreenMind to start at system login."""
    from screenmind.startup import install_startup
    ok = install_startup()
    return {"ok": ok, "message": "Registered in system startup" if ok else "Failed to register"}


@router.post("/startup/uninstall")
async def uninstall_startup_route():
    """Remove ScreenMind from system startup."""
    from screenmind.startup import uninstall_startup
    ok = uninstall_startup()
    return {"ok": ok, "message": "Removed from system startup" if ok else "Failed to remove"}


@router.post("/shutdown")
async def shutdown_server(request: Request):
    """Gracefully shut down ScreenMind. Restricted to localhost."""
    import asyncio
    import os
    import signal
    import logging

    # Security: only allow shutdown from localhost
    client_host = request.client.host if request.client else None
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Shutdown only allowed from localhost")

    logger = logging.getLogger("screenmind.api")
    logger.info("Shutdown requested via API")

    async def _delayed_shutdown():
        await asyncio.sleep(0.5)  # let the response reach the client
        from screenmind.api import dependencies
        if dependencies.request_shutdown is not None:
            dependencies.request_shutdown()
            return
        # Use SIGINT for clean shutdown — allows uvicorn to run cleanup,
        # close DB connections, stop llama-server, flush logs, run atexit.
        if sys.platform == "win32":
            # Windows: SIGINT to self doesn't work reliably, use CTRL_C_EVENT
            os.kill(os.getpid(), signal.CTRL_C_EVENT)
        else:
            os.kill(os.getpid(), signal.SIGINT)

    asyncio.create_task(_delayed_shutdown())
    return {"ok": True, "message": "ScreenMind is shutting down..."}
