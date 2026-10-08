"""
ScreenMind Configuration
Centralized settings using pydantic-settings. 
All values can be overridden via .env file or environment variables.
Runtime changes (from dashboard) are persisted to settings.json.
"""

import json
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List, Literal, Optional

from pydantic_settings import BaseSettings
from pydantic import Field, PrivateAttr, ValidationError



# ── Logging Setup ────────────────────────────────────────────────────────────

_LOG_FORMAT = logging.Formatter(
    "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# About 250 KB a day at INFO, so 1 MB x (1 + 3 backups) keeps about two weeks
LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUPS = 3


def _setup_logging():
    """Configure screenmind logger hierarchy. Safe to call multiple times.

    Logs to stderr when there is one. The log file is added by setup_file_log()
    at app start, not here: this runs on every import (tests, scripts).

    Environment variables:
        SCREENMIND_LOG_LEVEL: DEBUG, INFO (default), WARNING, ERROR
    """
    root = logging.getLogger("screenmind")
    if root.handlers:
        return
    level = os.environ.get("SCREENMIND_LOG_LEVEL", "INFO")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # pythonw has no stderr (None) until __main__.py points it at devnull
    if sys.stderr is not None:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(_LOG_FORMAT)
        root.addHandler(stderr_handler)


def setup_file_log(data_path: Path) -> Optional[Path]:
    """Also log to a rotating file. Returns its path, or None if it can't be opened.

    The file is SCREENMIND_LOG_FILE if set, else <data dir>/screenmind.log.
    main.run() calls it on every app start, so starts without a console
    (pythonw, the launcher, start at login) leave a log too. Safe to call twice.
    """
    path = Path(os.environ.get("SCREENMIND_LOG_FILE") or Path(data_path) / "screenmind.log")
    root = logging.getLogger("screenmind")
    for h in root.handlers:
        if isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == path.resolve():
            return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS,
                                      encoding="utf-8")
    except OSError as e:
        root.warning("Could not open log file %s: %s", path, e)
        return None
    handler.setFormatter(_LOG_FORMAT)
    root.addHandler(handler)
    return path

_setup_logging()

logger = logging.getLogger("screenmind.config")

# Settings keys that can be overridden at runtime (from dashboard or settings.json)
_ALLOWED_OVERRIDES = {
    "capture_interval", "performance_mode",
    "context_window", "kv_cache_quant", "flash_attention",
    "analysis_mode",
    "auto_pause_heavy_apps", "heavy_apps",
    "defer_analysis", "meeting_transcription",
    "meeting_apps", "call_end_grace_s",
    "active_model", "model_variants", "retention_days",
    "sensitive_filter_enabled", "sensitive_filter_types",
    "encryption_enabled",
    "capture_active_monitor",
    "setup_complete",
    "capture_paused",
    "ui_events_enabled", "ui_events_types", "event_triggered_capture",
}

# Runtime state, not configuration: left out of the non-default settings list
_STATE_KEYS = {"setup_complete", "capture_paused"}

def is_frozen() -> bool:
    """True inside the PyInstaller app (ScreenMind.exe, ScreenMind.app).

    There is no Python checkout there: sys.executable is the app itself and
    the package lives inside the bundle. See docs/plans/packaging.md (F1-F5).
    """
    return bool(getattr(sys, "frozen", False))


def _env_file() -> Path:
    """The .env to read.

    From source: the checkout's .env, not the current directory's: a start at
    login runs in another directory (/ for the macOS LaunchAgent) and missed it.
    In the app: the data dir's .env (optional), never one from the cwd or the
    bundle. The data dir is DATA_DIR from the environment, else ~/.screenmind.
    """
    if is_frozen():
        data_dir = os.environ.get("DATA_DIR") or os.environ.get("data_dir") or "~/.screenmind"
        return Path(os.path.expanduser(data_dir)) / ".env"
    return Path(__file__).resolve().parents[1] / ".env"


_ENV_FILE = _env_file()

# Lock to prevent concurrent read-modify-write races on settings.json
_settings_lock = threading.Lock()

class Settings(BaseSettings):
    """Application settings loaded from environment / .env file."""

    # ── Capture ──────────────────────────────────────────────────────────
    capture_interval: int = Field(
        default=10,
        description="Seconds between screenshot captures",
        ge=10,
        le=120,
    )
    screenshot_quality: int = Field(
        default=70,
        description="JPEG quality (1-100). 70 balances size vs readability",
        ge=10,
        le=100,
    )
    data_dir: str = Field(
        default="~/.screenmind",
        description="Root directory for all ScreenMind data",
    )
    capture_active_monitor: bool = Field(
        default=False,
        description="(Beta) Capture the monitor with the active window instead of primary",
    )
    capture_all_monitors: bool = Field(
        default=True,
        description="With several displays, capture each one every tick (one timeline entry per display). Off = one display only.",
    )
    capture_paused: bool = Field(
        default=True,
        description="Persisted capture state. True = paused (default for fresh installs), False = capturing.",
    )
    capture_on_start: bool = Field(
        default=False,
        description="Always start capturing on launch, ignoring the persisted capture_paused state.",
    )

    # ── Model ────────────────────────────────────────────────────────────
    active_model: str = Field(
        default="gemma-4-e2b",
        description="Active model key for llama-server",
    )
    model_variants: dict = Field(
        default_factory=dict,
        description="Per-model variant selection, e.g. {'gemma-4-e2b': 'Q8_0'}",
    )
    llama_server_host: str = Field(
        default="http://127.0.0.1:5809",
        description="llama-server URL",
    )
    llama_server_port: int = Field(
        default=5809,
        description="llama-server port",
    )
    llama_server_shared: bool = Field(
        default=False,
        description="Another process owns llama-server: use it if it runs, never start or stop it. "
                    "Set by scripts/dev-instance.sh.",
    )
    # ── Privacy ──────────────────────────────────────────────────────────
    blocked_apps: str = Field(
        default="",
        description="Comma-separated app names to skip (privacy zones)",
    )

    # ── Resource Management ──────────────────────────────────────────────
    performance_mode: Literal["minimal", "balanced", "maximum"] = Field(
        default="balanced",
        description="'minimal' (CPU-heavy, saves VRAM), 'balanced' (default), 'maximum' (full GPU)",
    )
    context_window: int = Field(
        default=6144,
        description="Context window size for llama-server (-c flag). Lower saves VRAM.",
        ge=2048,
        le=16384,
    )
    analysis_mode: Literal["fast", "balanced", "merged"] = Field(
        default="fast",
        description="'fast' (~12s, no thinking), 'balanced' (~40s, thinking), or 'merged' (~76s, thinking+layout)",
    )
    kv_cache_quant: bool = Field(
        default=False,
        description="Enable KV cache quantization (saves ~200MB VRAM but adds ~10s per inference)",
    )
    flash_attention: bool = Field(
        default=True,
        description="Enable flash attention (faster, less VRAM). Disable if GPU doesn't support it.",
    )
    auto_pause_heavy_apps: bool = Field(
        default=True,
        description="Auto-pause capture when heavy apps (games, editors) are in foreground",
    )
    heavy_apps: str = Field(
        default="game,valorant,fortnite,minecraft,unity,unreal,premiere,resolve,blender,obs,davinci",
        description="Comma-separated substrings to match against foreground app names",
    )
    defer_analysis: bool = Field(
        default=False,
        description="When ON, queue screenshots and analyze only when idle (60s no new captures)",
    )

    # ── Meeting Transcription ────────────────────────────────────────────
    meeting_transcription: bool = Field(
        default=False,
        description="Transcribe and summarize detected calls (calls are tracked either way)",
    )
    meeting_apps: str = Field(
        default="zoom,teams,meet,webex,slack,discord",
        description="Call apps to detect. Built-in rules for zoom, teams, meet, webex, slack, "
                    "discord; any other entry matches a window owner or title containing it",
    )
    call_end_grace_s: int = Field(
        default=120,
        ge=10,
        le=1800,
        description="A call ends after it has not been seen for this many seconds. A call "
                    "seen again within this time (also after a restart) keeps its row",
    )
    # ── UI Events (accessibility) ───────────────────────────────────────
    ui_events_enabled: bool = Field(
        default=True,
        description="Record clicks, app switches, typed text and clipboard via OS "
                    "accessibility APIs. macOS and Windows.",
    )
    # Typed text and clipboard are on by default (the user's call, 2026-10-08).
    # They can hold private content: the sensitive-data filter and the
    # password-field rules apply, but there is no PII detection yet (G31).
    ui_events_types: str = Field(
        default="click,app_switch,text,clipboard",
        description="Comma-separated UI event types to record: click, app_switch, window_focus, text, clipboard",
    )
    event_triggered_capture: bool = Field(
        default=True,
        description="Take a screenshot right after app switches, clicks, typing pauses and copies "
                    "(only when ui_events_enabled is on)",
    )

    # ── OCR ───────────────────────────────────────────────────────────────
    # One fixed default rather than one from the OS languages, so every machine
    # gets the same recognizer and the same text (G32). It selects the East
    # Slavic model, which reads English and the same accented Latin as the
    # English one, plus Cyrillic, at the same cost per frame.
    ocr_languages: str = Field(
        default="en,es,de,fr,ru",
        description="Comma-separated language codes, e.g. 'en,ru' or 'en,es,de'. OCR reads one "
                    "script per frame: a Cyrillic code wins, else the first non-English code "
                    "(ru/uk/be: East Slavic, es/de/fr...: Latin). Every script model also reads English.",
    )
    ocr_threads: int = Field(
        default=2,
        ge=0,
        description="CPU threads per OCR model run. 2 uses about 40% less CPU per frame than "
                    "all cores, but each frame takes longer. 0 = onnxruntime's default (all cores).",
    )

    # ── Privacy & Security ────────────────────────────────────────────────
    sensitive_filter_enabled: bool = Field(default=True, description="Filter sensitive data (cards, SSNs, API keys, passwords, emails, phones, IBANs) from captured text")
    sensitive_filter_types: str = Field(default="credit_card,ssn,api_key,jwt,password,email,phone,iban", description="Comma-separated filter types (privacy.data_filter.PATTERNS)")
    encryption_enabled: bool = Field(default=False, description="Encrypt screenshots at rest (AES via OS keyring)")

    # ── Data Retention ───────────────────────────────────────────────────
    retention_days: int = Field(
        default=7,
        description="Auto-delete timeline data older than N days. 0 = keep forever.",
        ge=0,
        le=365,
    )

    # ── Server ───────────────────────────────────────────────────────────
    api_host: str = Field(default="127.0.0.1", description="API bind host")
    api_port: int = Field(default=7777, description="API bind port")

    # ── Tray icon (Windows) ──────────────────────────────────────────────
    tray_icon: Optional[bool] = Field(
        default=None,
        description="Tray icon on Windows: status, pause or resume, open dashboard, quit. "
                    "Unset: on in the app (ScreenMind.exe), off from source. "
                    "TRAY_ICON=true turns it on from source, false turns it off in the app.",
    )

    # ── Internal State ────────────────────────────────────────────────────
    setup_complete: bool = Field(default=False, description="Whether first-run setup is complete")

    # Values before settings.json (defaults, .env, env vars), and the keys
    # settings.json set. See save_runtime_overrides() and non_defaults().
    _base: dict = PrivateAttr(default_factory=dict)
    _from_json: set = PrivateAttr(default_factory=set)

    model_config = {
        "env_file": _ENV_FILE,
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        # A .env can still hold keys of removed features (BOOKMARK_HOTKEY,
        # WORKSPACE_DIRS, GEMMA_MODE...). Ignore them instead of failing.
        "extra": "ignore",
        "validate_assignment": True,
    }

    # ── Derived Paths ────────────────────────────────────────────────────

    @property
    def data_path(self) -> Path:
        """Resolved, absolute data directory path."""
        return Path(os.path.expanduser(self.data_dir)).resolve()

    @property
    def screenshots_dir(self) -> Path:
        """Directory where screenshots are stored, organized by date."""
        return self.data_path / "screenshots"

    @property
    def db_path(self) -> Path:
        """SQLite database file path."""
        return self.data_path / "screenmind.db"

    @property
    def settings_json_path(self) -> Path:
        """Path to runtime settings override file."""
        return self.data_path / "settings.json"

    @property
    def ocr_languages_list(self) -> List[str]:
        """Parsed list of OCR language codes; empty means the default."""
        langs = [l.strip() for l in self.ocr_languages.split(",") if l.strip()]
        return langs or type(self).model_fields["ocr_languages"].default.split(",")

    @property
    def blocked_apps_list(self) -> List[str]:
        """Parsed list of blocked app names."""
        if not self.blocked_apps:
            return []
        return [a.strip().lower() for a in self.blocked_apps.split(",") if a.strip()]

    @property
    def ui_events_types_list(self) -> List[str]:
        """Parsed list of enabled UI event types."""
        return [t.strip().lower() for t in self.ui_events_types.split(",") if t.strip()]

    @property
    def heavy_apps_list(self) -> List[str]:
        """Parsed list of heavy app substrings for auto-pause."""
        if not self.heavy_apps:
            return []
        return [a.strip().lower() for a in self.heavy_apps.split(",") if a.strip()]

    @property
    def meeting_apps_list(self) -> List[str]:
        """Parsed list of meeting app substrings."""
        if not self.meeting_apps:
            return []
        return [a.strip().lower() for a in self.meeting_apps.split(",") if a.strip()]

    @property
    def tray_icon_on(self) -> bool:
        """Whether to show the tray icon (Windows only; see tray_icon)."""
        return is_frozen() if self.tray_icon is None else self.tray_icon

    @property
    def num_gpu_layers(self) -> int:
        """Map performance_mode to GPU layers for llama-server (-ngl flag)."""
        mode_map = {"minimal": 0, "balanced": 15, "maximum": 99}
        return mode_map.get(self.performance_mode, 15)

    def model_post_init(self, __context):
        self._base = {k: getattr(self, k) for k in _ALLOWED_OVERRIDES if hasattr(self, k)}

    def non_defaults(self) -> list:
        """(name, value, source) for each setting that differs from its code default.

        source is "settings.json", "env" (an environment variable) or ".env".
        Runtime state (setup_complete, capture_paused) is left out.
        """
        env_keys = {k.lower() for k in os.environ}
        out = []
        for name, field in type(self).model_fields.items():
            value = getattr(self, name)
            if name in _STATE_KEYS or value == field.get_default(call_default_factory=True):
                continue
            if name in self._from_json:
                source = "settings.json"
            elif name in env_keys:
                source = "env"
            else:
                source = ".env"
            out.append((name, value, source))
        return out

    def describe_non_defaults(self) -> str:
        """One line for the startup log: 'key=value (source); ...' or 'none'."""
        parts = []
        for name, value, source in self.non_defaults():
            text = str(value)
            if len(text) > 60:
                text = text[:57] + "..."
            parts.append(f"{name}={text} ({source})")
        return "; ".join(parts) or "none"

    def ensure_dirs(self):
        """Create all required directories if they don't exist."""
        self.data_path.mkdir(parents=True, exist_ok=True)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)

    def load_runtime_overrides(self):
        """Load settings.json overrides (from dashboard changes)."""
        path = self.settings_json_path
        if path.exists():
            try:
                with _settings_lock:
                    overrides = json.loads(path.read_text())
                applied = []
                for k, v in overrides.items():
                    if k in _ALLOWED_OVERRIDES and hasattr(self, k):
                        try:
                            setattr(self, k, v)
                            applied.append(k)
                        except (ValueError, ValidationError):
                            logger.warning("Invalid override ignored: %s=%r", k, v)
                self._from_json.update(applied)
                # main() logs the settings that differ from the defaults
                logger.debug("Loaded runtime overrides: %s", applied)
            except Exception as e:
                logger.error(f"Failed to load settings.json: {e}")

    def save_runtime_overrides(self, updates: dict):
        """Save dashboard settings to settings.json.

        A value equal to the one without settings.json (code default, .env or
        env var) is removed from the file instead of stored, so it follows
        later default changes. The dashboard posts its whole form on every
        save; storing all of it froze each default of that day per machine.
        """
        with _settings_lock:
            path = self.settings_json_path
            existing = {}
            if path.exists():
                try:
                    existing = json.loads(path.read_text())
                except Exception as e:
                    logger.debug("Could not read existing settings.json: %s", e)
            for k, v in updates.items():
                if k not in _ALLOWED_OVERRIDES:
                    continue
                existing[k] = v
                if not hasattr(self, k):
                    continue
                try:
                    setattr(self, k, v)
                except (ValueError, ValidationError) as e:
                    logger.debug("Override not applied in memory: %s=%r, %s", k, v, e)
                    continue
                if k in self._base and getattr(self, k) == self._base[k]:
                    del existing[k]
                    self._from_json.discard(k)
                else:
                    self._from_json.add(k)
            path.write_text(json.dumps(existing, indent=2))


# Singleton instance
settings = Settings()
# Load runtime overrides from settings.json (if any)
settings.load_runtime_overrides()
