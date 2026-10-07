"""Tests for storage/database.py — SQLite operations."""

from datetime import datetime

from screenmind.storage.models import ScreenshotEntry, ActivityRecord


def test_insert_and_get_activity(db):
    entry = ScreenshotEntry(
        timestamp=datetime(2026, 5, 16, 10, 30, 0),
        screenshot_path="/tmp/test.jpg",
        window_title="VS Code",
        detected_app_name="Code",
        analyzed=False,
    )
    activity_id = db.insert_activity(entry)
    assert activity_id is not None
    assert activity_id > 0

    # Retrieve it
    activity = db.get_activity_by_id(activity_id)
    assert activity is not None
    assert activity["window_title"] == "VS Code"
    assert activity["detected_app"] == "Code"


def test_get_activities_by_date(db):
    for hour in range(3):
        entry = ScreenshotEntry(
            timestamp=datetime(2026, 5, 16, 10 + hour, 0, 0),
            screenshot_path=f"/tmp/test_{hour}.jpg",
            analyzed=False,
        )
        db.insert_activity(entry)

    activities = db.get_activities_by_date("2026-05-16")
    assert len(activities) == 3


def test_get_activities_empty_date(db):
    activities = db.get_activities_by_date("2099-01-01")
    assert activities == []


def test_update_activity_analysis(db):
    entry = ScreenshotEntry(
        timestamp=datetime(2026, 5, 16, 14, 0, 0),
        screenshot_path="/tmp/analysis.jpg",
        analyzed=False,
    )
    aid = db.insert_activity(entry)

    analysis = ActivityRecord(
        app_name="Chrome",
        activity_category="browsing",
        activity_summary="Reading docs",
        mood="learning",
        confidence=0.85,
    )
    db.update_activity_analysis(aid, analysis)

    activity = db.get_activity_by_id(aid)
    assert activity["app_name"] == "Chrome"
    assert activity["category"] == "browsing"
    assert activity["summary"] == "Reading docs"
    assert activity["analyzed"] == 1


def test_delete_by_date(db):
    for i in range(5):
        entry = ScreenshotEntry(
            timestamp=datetime(2026, 5, 16, 10 + i, 0, 0),
            screenshot_path=f"/tmp/del_{i}.jpg",
            analyzed=False,
        )
        db.insert_activity(entry)

    deleted = db.delete_by_date("2026-05-16")
    assert deleted == 5

    activities = db.get_activities_by_date("2026-05-16")
    assert len(activities) == 0


def test_get_stats(db):
    for i, cat in enumerate(["coding", "coding", "browsing"]):
        entry = ScreenshotEntry(
            timestamp=datetime(2026, 5, 16, 10 + i, 0, 0),
            screenshot_path=f"/tmp/stat_{i}.jpg",
            analyzed=False,
        )
        aid = db.insert_activity(entry)
        analysis = ActivityRecord(
            app_name="App",
            activity_category=cat,
            activity_summary="test",
        )
        db.update_activity_analysis(aid, analysis)

    stats = db.get_stats("2026-05-16", "2026-05-16")
    assert stats["total_activities"] == 3
