"""Move the data of ScreenMind, the old name of this app (before 0.3.0).

The first start of 0.3.0 renames ~/.screenmind to ~/.evolve-capture
(%USERPROFILE%\\.screenmind on Windows). The steps:

1. migrate_data_dir(), before the DB opens and before any worker starts:
   writes a marker into the old dir and renames the dir in one step (same
   volume). It leaves a symlink at the old path where the OS allows it, so
   tools that read ~/.screenmind still find the data. It does nothing when
   DATA_DIR is set, when the new dir already holds data (both are kept, never
   merged), or when another instance is running.
2. finish_pending(), on every start: if the marker is there, it renames
   screenmind.db and screenmind.log, rewrites the absolute screenshot paths
   in the DB (they start with the old dir) and deletes the marker. A crash
   halfway is safe: the next start finishes the job.

Keep this module light: __main__.py and config.py import it before anything
else, and it must not import config.
"""

import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Callable, List

logger = logging.getLogger("evolve_capture.legacy")

OLD_DIR_NAME = ".screenmind"
NEW_DIR_NAME = ".evolve-capture"
MARKER = ".moving-from-screenmind"

OLD_DB, NEW_DB = "screenmind.db", "evolve-capture.db"
OLD_LOG, NEW_LOG = "screenmind.log", "evolve-capture.log"


def old_data_dir() -> Path:
    return Path.home() / OLD_DIR_NAME


def new_data_dir() -> Path:
    return Path.home() / NEW_DIR_NAME


def _pending(old: Path, new: Path) -> bool:
    """True while the old dir is still a real dir and the new one is missing."""
    return old.is_dir() and not old.is_symlink() and not new.exists()


def shared_home() -> Path:
    """The default data dir as it is right now.

    The models, the launcher lock and the crash log live here, also for dev
    instances with their own DATA_DIR. Until the move has run, that is the old
    dir: using the new one would create it, and the move would then see two
    dirs and keep the empty one.
    """
    old, new = old_data_dir(), new_data_dir()
    return old if _pending(old, new) else new


def _is_empty_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink() and not any(path.iterdir())


def migrate_data_dir(settings, instance_running: Callable[[], bool]) -> List[str]:
    """Move ~/.screenmind to ~/.evolve-capture. Returns lines for the log.

    settings is config.settings. On success it reloads settings.json from the
    new dir. If the rename fails, it points settings at the old dir for this
    run, so the app still starts with the user's data.
    """
    default = type(settings).model_fields["data_dir"].default
    if settings.data_dir != default:
        return []  # DATA_DIR is set: the user chose the dir, don't move anything
    old, new = old_data_dir(), Path(settings.data_path)
    if not old.is_dir() or old.is_symlink():
        return []
    if _is_empty_dir(new):
        new.rmdir()
    if new.exists():
        msg = (f"Both {old} and {new} exist. Using {new}; {old} is left as it is "
               f"(not merged). Move or delete it by hand.")
        logger.warning(msg)
        return [msg]
    if instance_running():
        msg = f"Not moving {old} to {new}: another instance is running. Stop it and start again."
        logger.warning(msg)
        return [msg]

    marker = old / MARKER
    try:
        marker.write_text(str(old.resolve()), encoding="utf-8")
        os.rename(old, new)
    except OSError as e:
        marker.unlink(missing_ok=True)
        settings.data_dir = str(old)
        settings.load_runtime_overrides()
        msg = f"Could not move {old} to {new}: {e}. Using {old} for now."
        logger.error(msg)
        return [msg]

    lines = [f"Moved {old} to {new} (ScreenMind is now Evolve Capture)"]
    if sys.platform != "win32":
        try:
            old.symlink_to(new, target_is_directory=True)
            lines.append(f"Left a symlink {old} -> {new}")
        except OSError as e:
            lines.append(f"No symlink at {old}: {e}")
    settings.load_runtime_overrides()
    for line in lines:
        logger.info(line)
    return lines


def _rename(data: Path, old_name: str, new_name: str, lines: List[str]):
    src, dst = data / old_name, data / new_name
    if src.exists() and not dst.exists():
        os.rename(src, dst)
        lines.append(f"Renamed {old_name} to {new_name}")


def _rewrite_paths(db: Path, old_prefix: str, new_prefix: str) -> int:
    """Rewrite screenshot paths that start with old_prefix. Returns the row count.

    The FTS update trigger is dropped for the update and created again in the
    same transaction: the FTS index has no paths, and re-indexing every row
    would only cost time.
    """
    conn = sqlite3.connect(str(db), timeout=30)
    try:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='activities'").fetchone()
        if not has_table:
            return 0
        trigger = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='activities_fts_au'"
        ).fetchone()
        n = len(old_prefix)
        with conn:
            if trigger:
                conn.execute("DROP TRIGGER activities_fts_au")
            cur = conn.execute(
                "UPDATE activities SET screenshot_path = ? || substr(screenshot_path, ?) "
                "WHERE substr(screenshot_path, 1, ?) = ?",
                (new_prefix, n + 1, n, old_prefix),
            )
            if trigger:
                conn.execute(trigger[0])
        return cur.rowcount
    finally:
        conn.close()


def finish_pending(data_path: Path) -> List[str]:
    """Finish a move that migrate_data_dir() started. Returns lines for the log.

    Safe to run again after a crash: each step skips what is done.
    """
    data = Path(data_path)
    marker = data / MARKER
    if not marker.exists():
        return []
    old_prefix = marker.read_text(encoding="utf-8").strip().rstrip("/\\") + os.sep
    new_prefix = str(data.resolve()) + os.sep
    lines: List[str] = []
    for suffix in ("-wal", "-shm"):
        # A WAL that was not checkpointed belongs to the DB: rename it first
        _rename(data, OLD_DB + suffix, NEW_DB + suffix, lines)
    _rename(data, OLD_DB, NEW_DB, lines)
    _rename(data, OLD_LOG, NEW_LOG, lines)
    db = data / NEW_DB
    if db.exists() and old_prefix != new_prefix:
        rows = _rewrite_paths(db, old_prefix, new_prefix)
        lines.append(f"Rewrote {rows} screenshot paths from {old_prefix} to {new_prefix}")
    marker.unlink()
    for line in lines:
        logger.info(line)
    return lines
