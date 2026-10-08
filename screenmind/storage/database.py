"""
SQLite Database Module
Handles all database operations for ScreenMind.
Schema creation and CRUD for activities, UI events and meetings.
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Any

from screenmind.config import settings
from screenmind.storage.models import ActivityRecord, ScreenshotEntry

logger = logging.getLogger("screenmind.storage.database")


class Database:
    """
    Thread-safe SQLite database for ScreenMind activity storage.
    Uses WAL mode for concurrent read/write support.
    """

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = db_path or settings.db_path
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a thread-local database connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
            )
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA foreign_keys=ON")
        return self._local.conn

    # ── FTS5 schema (single source of truth) ─────────────────────────────

    _FTS5_COLUMNS = "summary, details, ocr_text, app_name, scene_description, organized_text, user_actions"

    def _recreate_fts(self, conn: sqlite3.Connection):
        """Drop and recreate the FTS5 virtual table + sync triggers.
        
        Wrapped in BEGIN IMMEDIATE so concurrent connections never observe
        the table in a dropped state (triggers would fail their parent DML).
        """
        cols = self._FTS5_COLUMNS
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Drop old triggers first (they reference the table)
            conn.execute("DROP TRIGGER IF EXISTS activities_fts_ai")
            conn.execute("DROP TRIGGER IF EXISTS activities_fts_ad")
            conn.execute("DROP TRIGGER IF EXISTS activities_fts_au")
            conn.execute("DROP TABLE IF EXISTS activities_fts")
            conn.execute(f"""
                CREATE VIRTUAL TABLE activities_fts USING fts5(
                    {cols}, content='activities', content_rowid='id'
                )
            """)
            # Canonical FTS5 sync triggers — always use old.* for deletes
            conn.execute(f"""CREATE TRIGGER activities_fts_ai AFTER INSERT ON activities BEGIN
                INSERT INTO activities_fts(rowid, {cols})
                VALUES (new.id, new.summary, new.details, new.ocr_text, new.app_name, new.scene_description, new.organized_text, new.user_actions);
            END""")
            conn.execute(f"""CREATE TRIGGER activities_fts_ad AFTER DELETE ON activities BEGIN
                INSERT INTO activities_fts(activities_fts, rowid, {cols})
                VALUES ('delete', old.id, old.summary, old.details, old.ocr_text, old.app_name, old.scene_description, old.organized_text, old.user_actions);
            END""")
            conn.execute(f"""CREATE TRIGGER activities_fts_au AFTER UPDATE ON activities BEGIN
                INSERT INTO activities_fts(activities_fts, rowid, {cols})
                VALUES ('delete', old.id, old.summary, old.details, old.ocr_text, old.app_name, old.scene_description, old.organized_text, old.user_actions);
                INSERT INTO activities_fts(rowid, {cols})
                VALUES (new.id, new.summary, new.details, new.ocr_text, new.app_name, new.scene_description, new.organized_text, new.user_actions);
            END""")
            conn.execute("INSERT INTO activities_fts(activities_fts) VALUES('rebuild')")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _init_db(self):
        """Create tables and indexes if they don't exist."""
        settings.ensure_dirs()
        conn = self._get_conn()
        conn.executescript(
            """
            -- Core activity records
            CREATE TABLE IF NOT EXISTS activities (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       DATETIME NOT NULL,
                screenshot_path TEXT NOT NULL,
                window_title    TEXT,
                detected_app    TEXT,
                app_name        TEXT,
                category        TEXT,
                summary         TEXT,
                details         TEXT,
                visible_text    TEXT,
                mood            TEXT,
                confidence      REAL,
                ocr_text        TEXT,
                ocr_boxes       TEXT,
                scene_description TEXT,
                organized_text  TEXT,
                analyzed        BOOLEAN DEFAULT 0,
                analysis_error  TEXT,
                analysis_method TEXT,
                status          TEXT DEFAULT 'pending',
                created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            -- Indexes for fast queries
            CREATE INDEX IF NOT EXISTS idx_activities_timestamp ON activities(timestamp);
            CREATE INDEX IF NOT EXISTS idx_activities_category ON activities(category);
            CREATE INDEX IF NOT EXISTS idx_activities_app ON activities(app_name);
            CREATE INDEX IF NOT EXISTS idx_activities_analyzed ON activities(analyzed);
        """
        )

        # Meeting transcripts table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meetings (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                start_time      DATETIME NOT NULL,
                end_time        DATETIME,
                app_name        TEXT,
                duration_minutes REAL DEFAULT 0,
                transcript      TEXT,
                summary         TEXT,
                created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── Versioned Migrations ─────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY
            )
        """)
        current = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0

        migrations = [
            # v1: add ocr_text column
            "ALTER TABLE activities ADD COLUMN ocr_text TEXT",
            # v2: add ocr_boxes column for search highlighting
            "ALTER TABLE activities ADD COLUMN ocr_boxes TEXT",
            # v3: add scene_description column
            "ALTER TABLE activities ADD COLUMN scene_description TEXT",
            # v4: add organized_text column
            "ALTER TABLE activities ADD COLUMN organized_text TEXT",
            # v5: add analysis_method column
            "ALTER TABLE activities ADD COLUMN analysis_method TEXT",
            # v6: add active_url column
            "ALTER TABLE activities ADD COLUMN active_url TEXT",
            # v7: add status column (pending/ok/skipped/failed/dead)
            # Replaces the ambiguous analyzed boolean for filtering real vs placeholder entries.
            [
                "ALTER TABLE activities ADD COLUMN status TEXT DEFAULT 'pending'",
                # Backfill: derive status from summary text (the reliable signal).
                # Do NOT use confidence as discriminator — early-adopter DBs may have
                # real analyses stored at confidence=0.0 before _normalize existed.
                """UPDATE activities SET status = CASE
                    WHEN analyzed = 1 AND summary LIKE 'Skipped (corrupt%' THEN 'dead'
                    WHEN analyzed = 1 AND summary LIKE 'Skipped (screenshot deleted%' THEN 'dead'
                    WHEN analyzed = 1 AND confidence IS NULL THEN 'dead'
                    WHEN analyzed = 1 AND summary = 'Skipped (analysis backlog)' THEN 'skipped'
                    WHEN analyzed = 1 AND summary LIKE 'Analysis failed%' THEN 'failed'
                    WHEN analyzed = 1 THEN 'ok'
                    ELSE 'pending'
                END WHERE status = 'pending'""",
                "CREATE INDEX IF NOT EXISTS idx_activities_status ON activities(status)",
            ],
            # v8: UI events (clicks, typed text, app switches, clipboard) captured
            # via accessibility APIs, plus a per-frame text summary for FTS.
            [
                """CREATE TABLE IF NOT EXISTS ui_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    type TEXT NOT NULL,
                    app_name TEXT,
                    window_title TEXT,
                    element_role TEXT,
                    element_name TEXT,
                    element_value TEXT,
                    text TEXT,
                    x INTEGER,
                    y INTEGER,
                    activity_id INTEGER REFERENCES activities(id) ON DELETE SET NULL
                )""",
                "CREATE INDEX IF NOT EXISTS idx_ui_events_ts ON ui_events(timestamp)",
                "CREATE INDEX IF NOT EXISTS idx_ui_events_activity ON ui_events(activity_id)",
                "ALTER TABLE activities ADD COLUMN user_actions TEXT",
            ],
            # v9: browser page URL per UI event (read from the browser, not OCR)
            "ALTER TABLE ui_events ADD COLUMN url TEXT",
            # v10: call window title and room URL (calls are tracked even
            # without transcription)
            [
                "ALTER TABLE meetings ADD COLUMN window_title TEXT",
                "ALTER TABLE meetings ADD COLUMN url TEXT",
            ],
            # v11: drop what removed features wrote: embeddings (semantic
            # search), bookmarks, git context and daily summaries. A fresh DB
            # never has them, so "no such column" is fine.
            [
                "DROP INDEX IF EXISTS idx_activities_bookmarked",
                "DROP TABLE IF EXISTS dev_contexts",
                "DROP TABLE IF EXISTS daily_summaries",
                "ALTER TABLE activities DROP COLUMN embedding",
                "ALTER TABLE activities DROP COLUMN bookmarked",
            ],
        ]

        for i, migration in enumerate(migrations, start=1):
            if i > current:
                statements = migration if isinstance(migration, list) else [migration]
                for sql in statements:
                    if "DROP COLUMN" in sql and sqlite3.sqlite_version_info < (3, 35, 0):
                        # SQLite gained DROP COLUMN in 3.35. Older runtimes keep
                        # the column; nothing writes it any more.
                        logger.warning("Migration %d: SQLite %s cannot drop columns, skipped: %s",
                                       i, sqlite3.sqlite_version, sql)
                        continue
                    try:
                        conn.execute(sql)
                    except Exception as e:
                        err_msg = str(e).lower()
                        if ("duplicate column" in err_msg or "already exists" in err_msg
                                or ("DROP COLUMN" in sql and "no such column" in err_msg)):
                            logger.debug("Migration %d: %s (already applied)", i, e)
                        else:
                            logger.error("Migration %d failed: %s — SQL: %s", i, e, sql[:100])
                            raise
                conn.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (?)", (i,))

        conn.commit()
        if current < len(migrations):
            logger.info(f"Migrated schema v{current} → v{len(migrations)}")

        # ── FTS5 setup (after migrations so all columns exist for rebuild) ───
        # Check if all 3 sync triggers exist (not just one, to catch partial creates)
        trigger_count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
            "AND name IN ('activities_fts_ai', 'activities_fts_ad', 'activities_fts_au')"
        ).fetchone()[0]

        if trigger_count < 3:
            try:
                self._recreate_fts(conn)
                logger.info("FTS5 table + triggers created, index rebuilt")
            except Exception as e:
                logger.error(f"FTS5 setup failed: {e}")
        else:
            # Triggers exist — verify FTS5 table is intact (not corrupted)
            try:
                conn.execute(
                    f"SELECT {self._FTS5_COLUMNS} FROM activities_fts LIMIT 0"
                )
            except Exception as e:
                logger.warning(f"FTS5 table damaged, rebuilding: {e}")
                try:
                    self._recreate_fts(conn)
                    logger.info("FTS5 rebuilt successfully")
                except Exception as e2:
                    logger.error(f"FTS5 rebuild failed: {e2}")

        logger.info(f"Initialized at {self._db_path}")

    # ── Activity CRUD ────────────────────────────────────────────────────

    def insert_activity(self, entry: ScreenshotEntry) -> int:
        """Insert a new activity. Returns the row id.

        Note: status is NOT written here — it relies on the DDL default ('pending').
        All real analysis finalization goes through update_activity_analysis(), which
        sets status appropriately. If a future code path needs to insert a pre-analyzed
        row, it must also set status via update_activity_analysis or raw SQL.
        """
        conn = self._get_conn()
        analysis = entry.analysis
        cursor = conn.execute(
            """
            INSERT INTO activities (
                timestamp, screenshot_path, window_title, detected_app,
                app_name, category, summary, details,
                visible_text, mood, confidence, scene_description,
                analyzed, analysis_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.timestamp.isoformat(),
                entry.screenshot_path,
                entry.window_title,
                entry.detected_app_name,
                analysis.app_name if analysis else None,
                analysis.activity_category if analysis else None,
                analysis.activity_summary if analysis else None,
                analysis.detailed_context if analysis else None,
                json.dumps(analysis.visible_text_snippets) if analysis else None,
                analysis.mood if analysis else None,
                analysis.confidence if analysis else None,
                analysis.scene_description if analysis else None,
                entry.analyzed,
                entry.analysis_error,
            ),
        )
        conn.commit()
        return cursor.lastrowid

    def update_activity_analysis(
        self,
        activity_id: int,
        analysis: ActivityRecord,
        ocr_text: Optional[str] = None,
        ocr_boxes: Optional[str] = None,
        organized_text: Optional[str] = None,
        analysis_method: Optional[str] = None,
        active_url: Optional[str] = None,
        status: str = "ok",
    ):
        """Update an existing activity with analysis results, OCR text, bounding boxes, and organized text."""
        conn = self._get_conn()
        conn.execute(
            """
            UPDATE activities SET
                app_name = ?, category = ?, summary = ?, details = ?,
                visible_text = ?, mood = ?, confidence = ?,
                ocr_text = ?, ocr_boxes = ?,
                scene_description = ?, organized_text = ?,
                analysis_method = ?, active_url = ?,
                analyzed = 1, status = ?, analysis_error = NULL
            WHERE id = ?
            """,
            (
                analysis.app_name,
                analysis.activity_category,
                analysis.activity_summary,
                analysis.detailed_context,
                json.dumps(analysis.visible_text_snippets),
                analysis.mood,
                analysis.confidence,
                ocr_text,
                ocr_boxes,
                analysis.scene_description,
                organized_text,
                analysis_method,
                active_url,
                status,
                activity_id,
            ),
        )
        # FTS5 sync is handled automatically by AFTER UPDATE trigger
        conn.commit()

    def mark_skipped(
        self,
        activity_id: int,
        ocr_text: Optional[str] = None,
        active_url: Optional[str] = None,
    ):
        """Finish a frame without analysis, keeping what was read at capture time.

        Analysis fields (category, summary, confidence...) stay NULL: nothing
        looked at the frame, and a made-up category would count as work time.
        user_actions is left as the capture worker wrote it. Backfill can still
        analyze the row later (status 'skipped').
        """
        conn = self._get_conn()
        conn.execute(
            """
            UPDATE activities SET
                app_name = COALESCE(app_name, detected_app),
                category = NULL, summary = NULL, details = NULL,
                visible_text = NULL, mood = NULL, confidence = NULL,
                scene_description = NULL, organized_text = NULL,
                ocr_text = ?, ocr_boxes = NULL, active_url = ?,
                analysis_method = 'skipped',
                analyzed = 1, status = 'skipped', analysis_error = NULL
            WHERE id = ?
            """,
            (ocr_text, active_url, activity_id),
        )
        conn.commit()

    def get_activities_by_date(
        self, target_date: str, limit: int = 200, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Get all activities for a specific date (YYYY-MM-DD)."""
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT a.*
            FROM activities a
            WHERE DATE(a.timestamp) = ?
            ORDER BY a.timestamp DESC
            LIMIT ? OFFSET ?
            """,
            (target_date, limit, offset),
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_activity_by_id(self, activity_id: int) -> Optional[Dict[str, Any]]:
        """Get a single activity by ID."""
        conn = self._get_conn()
        row = conn.execute(
            """
            SELECT a.*
            FROM activities a
            WHERE a.id = ?
            """,
            (activity_id,),
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def get_unanalyzed_activities(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get activities that haven't been analyzed yet."""
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT * FROM activities
            WHERE analyzed = 0
            ORDER BY timestamp ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    # ── Statistics ───────────────────────────────────────────────────────

    def get_stats(self, date_from: str, date_to: str) -> Dict[str, Any]:
        """Get aggregated statistics for a date range."""
        conn = self._get_conn()

        # Total activities
        total = conn.execute(
            "SELECT COUNT(*) as cnt FROM activities WHERE DATE(timestamp) BETWEEN ? AND ? AND status = 'ok'",
            (date_from, date_to),
        ).fetchone()["cnt"]

        # Category breakdown
        categories = conn.execute(
            """
            SELECT category, COUNT(*) as cnt
            FROM activities
            WHERE DATE(timestamp) BETWEEN ? AND ? AND status = 'ok'
            GROUP BY category
            ORDER BY cnt DESC
            """,
            (date_from, date_to),
        ).fetchall()

        # Top apps
        apps = conn.execute(
            """
            SELECT app_name, COUNT(*) as cnt
            FROM activities
            WHERE DATE(timestamp) BETWEEN ? AND ? AND status = 'ok'
            GROUP BY app_name
            ORDER BY cnt DESC
            LIMIT 10
            """,
            (date_from, date_to),
        ).fetchall()

        # Meetings stats
        try:
            meetings_row = conn.execute(
                """SELECT COUNT(*) as cnt, COALESCE(SUM(duration_minutes), 0) as total_mins
                   FROM meetings WHERE DATE(start_time) BETWEEN ? AND ?""",
                (date_from, date_to),
            ).fetchone()
            meetings_count = meetings_row["cnt"]
            meetings_minutes = meetings_row["total_mins"]
        except sqlite3.OperationalError as e:
            logger.warning("Meeting stats query failed: %s", e)
            meetings_count = 0
            meetings_minutes = 0

        # Status breakdown (analysis pipeline health)
        status_rows = conn.execute(
            """
            SELECT COALESCE(status, 'pending') as status, COUNT(*) as cnt
            FROM activities
            WHERE DATE(timestamp) BETWEEN ? AND ?
            GROUP BY status
            """,
            (date_from, date_to),
        ).fetchall()

        return {
            "total_activities": total,
            "category_breakdown": {r["category"]: r["cnt"] for r in categories},
            "top_apps": {r["app_name"]: r["cnt"] for r in apps},
            "meetings_count": meetings_count,
            "meetings_minutes": meetings_minutes,
            "status_breakdown": {r["status"]: r["cnt"] for r in status_rows},
        }

    def get_hourly_heatmap(self, date_from: str, date_to: str) -> List[Dict]:
        """Get activity counts by hour and day-of-week for heatmap."""
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT
                CAST(strftime('%w', timestamp) AS INTEGER) as day_of_week,
                CAST(strftime('%H', timestamp) AS INTEGER) as hour,
                COUNT(*) as cnt
            FROM activities
            WHERE DATE(timestamp) BETWEEN ? AND ? AND status = 'ok'
            GROUP BY day_of_week, hour
            """,
            (date_from, date_to),
        ).fetchall()
        return [dict(row) for row in rows]

    # ── Cleanup ──────────────────────────────────────────────────────────

    def delete_before(self, before_date: str) -> int:
        """Delete all activities before a date. Returns count deleted."""
        conn = self._get_conn()
        cursor = conn.execute(
            "DELETE FROM activities WHERE DATE(timestamp) < ?", (before_date,)
        )
        conn.commit()
        return cursor.rowcount

    def delete_by_date(self, target_date: str) -> int:
        """Delete all activities for a specific date. Also removes screenshot files. Returns count deleted."""
        conn = self._get_conn()
        # Get screenshot paths before deleting so we can remove files
        rows = conn.execute(
            "SELECT screenshot_path FROM activities WHERE DATE(timestamp) = ?",
            (target_date,),
        ).fetchall()
        # Delete from DB
        cursor = conn.execute(
            "DELETE FROM activities WHERE DATE(timestamp) = ?", (target_date,)
        )
        # Also remove meetings for this date
        conn.execute(
            "DELETE FROM meetings WHERE DATE(start_time) = ?", (target_date,)
        )
        conn.commit()
        # Remove screenshot files from disk
        for row in rows:
            try:
                p = Path(row["screenshot_path"])
                if p.exists():
                    p.unlink()
            except (OSError, PermissionError) as e:
                logger.debug("Cleanup skipped: %s", e)
        return cursor.rowcount

    def get_disk_usage(self) -> Dict[str, Any]:
        """Get database and screenshot disk usage stats."""
        db_size = self._db_path.stat().st_size if self._db_path.exists() else 0
        screenshot_size = sum(
            f.stat().st_size
            for f in settings.screenshots_dir.rglob("*.jpg")
            if f.is_file()
        )
        conn = self._get_conn()
        total = conn.execute("SELECT COUNT(*) as cnt FROM activities").fetchone()["cnt"]

        return {
            "db_size_mb": round(db_size / (1024 * 1024), 2),
            "screenshots_size_mb": round(screenshot_size / (1024 * 1024), 2),
            "total_activities": total,
        }

    def cleanup_old_data(self, retention_days: int) -> Dict[str, int]:
        """
        Delete all activities and meetings older than retention_days.
        Also removes screenshot files from disk.
        Returns counts of deleted items.
        """
        if retention_days <= 0:
            return {"activities": 0, "meetings": 0}

        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=retention_days)).strftime("%Y-%m-%d")

        conn = self._get_conn()

        # Get screenshot paths before deleting
        rows = conn.execute(
            "SELECT screenshot_path FROM activities WHERE DATE(timestamp) < ?",
            (cutoff,),
        ).fetchall()

        # Delete old activities
        act_cursor = conn.execute(
            "DELETE FROM activities WHERE DATE(timestamp) < ?", (cutoff,)
        )
        activities_deleted = act_cursor.rowcount

        # Delete old UI events
        conn.execute("DELETE FROM ui_events WHERE DATE(timestamp) < ?", (cutoff,))

        # Delete old meetings
        mtg_cursor = conn.execute(
            "DELETE FROM meetings WHERE DATE(start_time) < ?", (cutoff,)
        )
        meetings_deleted = mtg_cursor.rowcount

        conn.commit()

        # Remove screenshot files from disk
        for row in rows:
            try:
                p = Path(row["screenshot_path"])
                if p.exists():
                    p.unlink()
            except (OSError, PermissionError) as e:
                logger.debug("Cleanup skipped: %s", e)

        # Clean up empty date directories
        if settings.screenshots_dir.exists():
            for d in settings.screenshots_dir.iterdir():
                if d.is_dir() and not any(d.iterdir()):
                    try:
                        d.rmdir()
                    except (OSError, PermissionError) as e:
                        logger.debug("Could not remove empty dir: %s", e)

        return {"activities": activities_deleted, "meetings": meetings_deleted}

    def get_storage_estimate(self) -> Dict[str, Any]:
        """
        Calculate current daily storage rate and estimate storage for
        different retention periods.
        """
        conn = self._get_conn()

        # Get date range and total count
        stats = conn.execute("""
            SELECT COUNT(*) as total,
                   MIN(DATE(timestamp)) as first_date,
                   MAX(DATE(timestamp)) as last_date,
                   COUNT(DISTINCT DATE(timestamp)) as active_days
            FROM activities
        """).fetchone()

        total = stats["total"]
        active_days = stats["active_days"] or 1

        # Current disk usage
        usage = self.get_disk_usage()
        total_mb = usage["db_size_mb"] + usage["screenshots_size_mb"]

        # Average per day
        avg_mb_per_day = total_mb / max(active_days, 1)
        avg_activities_per_day = total / max(active_days, 1)

        return {
            "current_total_mb": round(total_mb, 1),
            "active_days": active_days,
            "avg_mb_per_day": round(avg_mb_per_day, 1),
            "avg_activities_per_day": round(avg_activities_per_day),
            "estimates": {
                "1": round(avg_mb_per_day * 1, 1),
                "7": round(avg_mb_per_day * 7, 1),
                "30": round(avg_mb_per_day * 30, 1),
                "90": round(avg_mb_per_day * 90, 1),
                "365": round(avg_mb_per_day * 365, 1),
            },
        }

    # ── Helpers ───────────────────────────────────────────────────────────

    def _row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        """Convert a SQLite Row to a dict with parsed JSON fields."""
        d = dict(row)
        # Parse JSON fields
        if d.get("visible_text"):
            try:
                d["visible_text"] = json.loads(d["visible_text"])
            except (json.JSONDecodeError, TypeError):
                d["visible_text"] = []
        return d

    # ── Meetings ─────────────────────────────────────────────────────────

    def insert_meeting(self, start_time, app_name, transcript="", summary="",
                       window_title=None, url=None) -> int:
        """Insert a new meeting record. Returns the meeting ID.
        transcript/summary are None for calls tracked without transcription."""
        conn = self._get_conn()
        cursor = conn.execute(
            """INSERT INTO meetings (start_time, app_name, transcript, summary, window_title, url)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (start_time.isoformat() if hasattr(start_time, 'isoformat') else start_time,
             app_name, transcript, summary, window_title, url),
        )
        conn.commit()
        return cursor.lastrowid

    def update_meeting_call_info(self, meeting_id: int, window_title=None, url=None,
                                 duration_minutes=None):
        """Fill in call details found after the start, and keep the running
        duration current so a crash does not lose it. NULL args are skipped."""
        conn = self._get_conn()
        conn.execute(
            """UPDATE meetings SET window_title = COALESCE(window_title, ?),
                   url = COALESCE(url, ?),
                   duration_minutes = COALESCE(?, duration_minutes)
               WHERE id = ?""",
            (window_title, url, duration_minutes, meeting_id),
        )
        conn.commit()

    def end_meeting(self, meeting_id: int, end_time, duration_minutes: float):
        """Close a call that has no transcript (transcription was off)."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE meetings SET end_time = ?, duration_minutes = ? WHERE id = ?",
            (end_time.isoformat() if hasattr(end_time, 'isoformat') else end_time,
             duration_minutes, meeting_id),
        )
        conn.commit()

    def get_latest_meeting(self) -> Optional[Dict[str, Any]]:
        """The most recent meetings row, or None."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM meetings ORDER BY start_time DESC, id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def reopen_meeting(self, meeting_id: int):
        """Mark an ended call as running again (it came back within the grace)."""
        conn = self._get_conn()
        conn.execute("UPDATE meetings SET end_time = NULL WHERE id = ?", (meeting_id,))
        conn.commit()

    def update_meeting(self, meeting_id: int, end_time=None, duration_minutes=0,
                       transcript="", summary=""):
        """Update a meeting with end time, transcript, and summary."""
        conn = self._get_conn()
        conn.execute(
            """UPDATE meetings SET end_time=?, duration_minutes=?, transcript=?, summary=?
               WHERE id=?""",
            (end_time.isoformat() if hasattr(end_time, 'isoformat') else end_time,
             duration_minutes, transcript, summary, meeting_id),
        )
        conn.commit()

    def get_meetings_by_date(self, target_date: str) -> List[Dict[str, Any]]:
        """Get all meetings for a specific date."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM meetings WHERE DATE(start_time) = ?
               ORDER BY start_time DESC""",
            (target_date,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_meeting_by_id(self, meeting_id: int) -> Optional[Dict[str, Any]]:
        """Get a single meeting by ID."""
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        return dict(row) if row else None

    def cleanup_stale_meetings(self) -> int:
        """
        Fix meetings left 'ongoing' from a previous crashed session.
        Sets end_time = start_time + the last saved duration and marks the
        summary as interrupted. Returns count of fixed meetings.
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, start_time, transcript, duration_minutes FROM meetings WHERE end_time IS NULL"
        ).fetchall()
        count = 0
        for row in rows:
            mid = row["id"]
            if row["transcript"] is None:
                summary = None  # tracked without transcription: nothing to summarize
            else:
                transcript = row["transcript"]
                summary = "(Session ended unexpectedly — no summary generated)"
                if transcript.strip() and transcript.strip() != "(No speech detected)":
                    summary = "⚠️ Session was interrupted. Click re-analyze (🔄) to generate summary."
            minutes = row["duration_minutes"] or 0
            end_time = row["start_time"]
            try:
                end_time = (datetime.fromisoformat(row["start_time"])
                            + timedelta(minutes=minutes)).isoformat()
            except (TypeError, ValueError):
                minutes = 0
            conn.execute(
                "UPDATE meetings SET end_time = ?, duration_minutes = ?, summary = ? WHERE id = ?",
                (end_time, minutes, summary, mid),
            )
            count += 1
        if count:
            conn.commit()
        return count

    def delete_meeting(self, meeting_id: int) -> bool:
        """Delete a meeting by ID. Returns True if deleted."""
        conn = self._get_conn()
        cursor = conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
        conn.commit()
        return cursor.rowcount > 0

    def update_meeting_summary(self, meeting_id: int, summary: str):
        """Update only the summary field of a meeting (for re-analysis)."""
        conn = self._get_conn()
        conn.execute("UPDATE meetings SET summary = ? WHERE id = ?", (summary, meeting_id))
        conn.commit()

    # ── UI Events ────────────────────────────────────────────────────────

    def insert_ui_events(self, events) -> int:
        """Batch insert UiEvent objects. Returns the number inserted."""
        if not events:
            return 0
        conn = self._get_conn()
        conn.executemany(
            """
            INSERT INTO ui_events (
                timestamp, type, app_name, window_title, element_role,
                element_name, element_value, text, x, y, url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    e.timestamp.isoformat(), e.type.value, e.app_name, e.window_title,
                    e.element_role, e.element_name, e.element_value, e.text, e.x, e.y,
                    e.url,
                )
                for e in events
            ],
        )
        conn.commit()
        return len(events)

    def attach_ui_events(self, activity_id: int, until: datetime) -> int:
        """Link all not-yet-linked events up to `until` to this activity."""
        conn = self._get_conn()
        cursor = conn.execute(
            "UPDATE ui_events SET activity_id = ? "
            "WHERE activity_id IS NULL AND timestamp <= ?",
            (activity_id, until.isoformat()),
        )
        conn.commit()
        return cursor.rowcount

    def get_ui_events(self, activity_id: int) -> List[Dict[str, Any]]:
        """Events linked to one activity, oldest first."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM ui_events WHERE activity_id = ? ORDER BY timestamp, id",
            (activity_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_ui_events_range(
        self, start: str, end: str, event_type: Optional[str] = None, limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """Events between two ISO timestamps (inclusive), oldest first."""
        conn = self._get_conn()
        sql = "SELECT * FROM ui_events WHERE timestamp >= ? AND timestamp <= ?"
        params: list = [start, end]
        if event_type:
            sql += " AND type = ?"
            params.append(event_type)
        sql += " ORDER BY timestamp, id LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def set_user_actions(self, activity_id: int, text: Optional[str]):
        """Store the human-readable action summary for a frame (indexed by FTS)."""
        conn = self._get_conn()
        conn.execute("UPDATE activities SET user_actions = ? WHERE id = ?", (text, activity_id))
        conn.commit()

    def close(self):
        """Close the database connection."""
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
