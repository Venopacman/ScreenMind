"""
Shared state and helpers for API route modules.
All route files import from here instead of accessing globals.
"""

import secrets
from datetime import datetime, timedelta
from typing import Optional

from screenmind.config import settings
from screenmind.engine.embedder import Embedder
from screenmind.storage.database import Database


# ── Session store (in-memory) ────────────────────────────────────────
_sessions = {}  # token -> expiry datetime


def verify_session(token: str) -> bool:
    """Check if a session token is valid and not expired."""
    if not token or token not in _sessions:
        return False
    if datetime.utcnow() > _sessions[token]:
        del _sessions[token]
        return False
    return True


def create_session() -> str:
    """Create a new session token."""
    token = secrets.token_hex(32)
    timeout = settings.dashboard_lock_timeout or 30
    _sessions[token] = datetime.utcnow() + timedelta(minutes=timeout)
    return token


def delete_session(token: str):
    """Remove a session token."""
    _sessions.pop(token, None)


# ── Shared instances (set by create_app) ─────────────────────────────
db: Optional[Database] = None
embedder: Optional[Embedder] = None
capture_worker = None
analysis_worker = None
audio_worker = None
ui_recorder = None  # UiEventRecorder, set by main after create_app




def init(database: Database, emb: Optional[Embedder], cap_worker, ana_worker, aud_worker):
    """Initialize shared state. Called once from create_app."""
    global db, embedder, capture_worker, analysis_worker, audio_worker
    db = database
    embedder = emb
    capture_worker = cap_worker
    analysis_worker = ana_worker
    audio_worker = aud_worker
