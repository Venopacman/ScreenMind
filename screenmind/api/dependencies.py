"""
Shared state and helpers for API route modules.
All route files import from here instead of accessing globals.
"""

from typing import Optional

from screenmind.engine.embedder import Embedder
from screenmind.storage.database import Database


# ── Shared instances (set by create_app) ─────────────────────────────
db: Optional[Database] = None
embedder: Optional[Embedder] = None
capture_worker = None
analysis_worker = None
audio_worker = None
ui_recorder = None  # UiEventRecorder, set by main after create_app
request_shutdown = None  # callable set by main: starts the same clean shutdown as Ctrl+C




def init(database: Database, emb: Optional[Embedder], cap_worker, ana_worker, aud_worker):
    """Initialize shared state. Called once from create_app."""
    global db, embedder, capture_worker, analysis_worker, audio_worker
    db = database
    embedder = emb
    capture_worker = cap_worker
    analysis_worker = ana_worker
    audio_worker = aud_worker
