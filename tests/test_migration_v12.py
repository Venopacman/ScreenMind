"""Tests for the v12 migration — "Analysis failed" rows stored as 'ok'.

Before analysis_worker stored them as 'failed', a llama-server outage left
rows with summary "Analysis failed..." and status 'ok'. The idle backfill
never retried them and feeds showed them as real analyses.
"""

import sqlite3

from screenmind.storage.database import Database


def _make_v11_db(path):
    Database(db_path=path).close()
    conn = sqlite3.connect(str(path))
    rows = [
        # (summary, status)
        ("Analysis failed", "ok"),
        ("Analysis failed: connection refused", "ok"),
        ("Editing database.py", "ok"),
        ("Analysis failed", "failed"),
        ("Skipped (corrupt screenshot)", "dead"),
        (None, "pending"),
    ]
    for i, (summary, status) in enumerate(rows):
        conn.execute(
            "INSERT INTO activities (timestamp, screenshot_path, app_name, summary, status) "
            "VALUES (?, ?, 'Code', ?, ?)",
            (f"2026-10-06T12:{40 + i}:00", f"/tmp/{i}.jpg", summary, status),
        )
    conn.execute("DELETE FROM schema_version WHERE version > 11")
    conn.commit()
    conn.close()


def _statuses(conn):
    return [r[0] for r in conn.execute("SELECT status FROM activities ORDER BY id")]


def test_v12_marks_failed_analyses_as_failed(tmp_path):
    path = tmp_path / "v11.db"
    _make_v11_db(path)

    db = Database(db_path=path)
    conn = db._get_conn()

    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] >= 12
    assert _statuses(conn) == ["failed", "failed", "ok", "failed", "dead", "pending"]


def test_v12_runs_twice_without_error(tmp_path):
    path = tmp_path / "again.db"
    _make_v11_db(path)
    Database(db_path=path).close()
    conn = sqlite3.connect(str(path))
    conn.execute("DELETE FROM schema_version WHERE version = 12")
    conn.commit()
    conn.close()

    db = Database(db_path=path)
    assert _statuses(db._get_conn()) == ["failed", "failed", "ok", "failed", "dead", "pending"]
