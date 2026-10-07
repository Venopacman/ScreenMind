"""Tests for the v11 migration — drops data of removed features.

Builds a DB as v10 left it (embedding and bookmarked columns, dev_contexts,
daily_summaries), then opens it with the current code.
"""

import sqlite3

from screenmind.storage.database import Database


def _make_v10_db(path):
    db = Database(db_path=path)
    db.close()
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        ALTER TABLE activities ADD COLUMN embedding BLOB;
        ALTER TABLE activities ADD COLUMN bookmarked BOOLEAN DEFAULT 0;
        CREATE INDEX idx_activities_bookmarked ON activities(bookmarked);
        CREATE TABLE dev_contexts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            activity_id INTEGER REFERENCES activities(id) ON DELETE CASCADE,
            repo_name TEXT, branch TEXT, last_commit TEXT, changed_files TEXT,
            insertions INTEGER DEFAULT 0, deletions INTEGER DEFAULT 0
        );
        CREATE INDEX idx_dev_repo ON dev_contexts(repo_name);
        CREATE TABLE daily_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date DATE UNIQUE NOT NULL, summary TEXT
        );
        INSERT INTO activities (timestamp, screenshot_path, app_name, summary, status,
                                embedding, bookmarked)
        VALUES ('2026-10-06T10:00:00', '/tmp/a.jpg', 'Slack', 'zebrafish standup', 'ok',
                X'00000000', 1);
        INSERT INTO dev_contexts (activity_id, repo_name) VALUES (1, 'ScreenMind');
        INSERT INTO daily_summaries (date, summary) VALUES ('2026-10-06', 'a day');
        DELETE FROM schema_version WHERE version > 10;
    """)
    conn.commit()
    conn.close()


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_v11_drops_removed_feature_data(tmp_path):
    path = tmp_path / "v10.db"
    _make_v10_db(path)

    db = Database(db_path=path)
    conn = db._get_conn()

    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 11
    assert not {"embedding", "bookmarked"} & _columns(conn, "activities")
    assert not {"dev_contexts", "daily_summaries"} & _tables(conn)
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE name = 'idx_activities_bookmarked'"
    ).fetchone() is None

    # The activity itself survives, and full-text search still finds it
    row = db.get_activity_by_id(1)
    assert row["app_name"] == "Slack"
    hits = conn.execute(
        "SELECT rowid FROM activities_fts WHERE activities_fts MATCH 'zebrafish'"
    ).fetchall()
    assert [h[0] for h in hits] == [1]


def test_fresh_db_has_no_removed_columns(tmp_path):
    db = Database(db_path=tmp_path / "fresh.db")
    conn = db._get_conn()
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 11
    assert not {"embedding", "bookmarked"} & _columns(conn, "activities")
    assert not {"dev_contexts", "daily_summaries"} & _tables(conn)


def test_v11_runs_twice_without_error(tmp_path):
    path = tmp_path / "again.db"
    Database(db_path=path).close()
    conn = sqlite3.connect(str(path))
    conn.execute("DELETE FROM schema_version WHERE version = 11")
    conn.commit()
    conn.close()
    db = Database(db_path=path)
    assert db._get_conn().execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 11
