"""
ScreenMind API Server
Creates the FastAPI app, mounts static files, and includes all route modules.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from screenmind.storage.database import Database

import logging
import screenmind.api.dependencies as deps

logger = logging.getLogger("screenmind.api.server")


def create_app(database: Database, capture_worker=None, analysis_worker=None, audio_worker=None):
    """Create and configure the FastAPI application."""

    app = FastAPI(title="ScreenMind", version="0.1.1")

    # Initialize shared dependencies for all route modules
    deps.init(database, capture_worker, analysis_worker, audio_worker)

    # ── Static Files ─────────────────────────────────────────────────
    static_dir = Path(__file__).parent / "static"
    app.mount("/css", StaticFiles(directory=str(static_dir / "css")), name="css")
    app.mount("/js", StaticFiles(directory=str(static_dir / "js")), name="js")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return (static_dir / "index.html").read_text(encoding="utf-8")

    # ── Include Route Modules ────────────────────────────────────────
    from screenmind.api.routes.auth import router as auth_router
    from screenmind.api.routes.capture import router as capture_router
    from screenmind.api.routes.timeline import router as timeline_router
    from screenmind.api.routes.search import router as search_router
    from screenmind.api.routes.stats import router as stats_router
    from screenmind.api.routes.screenshots import router as screenshots_router
    from screenmind.api.routes.meetings import router as meetings_router
    from screenmind.api.routes.settings import router as settings_router
    from screenmind.api.routes.models import router as models_router
    from screenmind.api.routes.data import router as data_router
    from screenmind.api.routes.ui_events import router as ui_events_router
    from screenmind.api.routes.export import router as export_router
    app.include_router(auth_router)
    app.include_router(capture_router)
    app.include_router(timeline_router)
    app.include_router(search_router)
    app.include_router(stats_router)
    app.include_router(screenshots_router)
    app.include_router(meetings_router)
    app.include_router(settings_router)
    app.include_router(ui_events_router)
    app.include_router(models_router)
    app.include_router(data_router)
    app.include_router(export_router)

    return app
