"""Extended database tests — covers methods missed by the original test_database.py."""
from datetime import datetime

from screenmind.storage.models import ScreenshotEntry, ActivityRecord


def test_get_unanalyzed_activities(db):
    """get_unanalyzed_activities returns only unanalyzed entries."""
    for i in range(3):
        entry = ScreenshotEntry(
            timestamp=datetime(2026, 5, 20, 10 + i, 0, 0),
            screenshot_path=f"/tmp/unanalyzed_{i}.jpg",
            analyzed=(i == 1),  # only middle one is analyzed
        )
        db.insert_activity(entry)

    unanalyzed = db.get_unanalyzed_activities(limit=10)
    assert len(unanalyzed) == 2


def test_get_hourly_heatmap(db):
    """get_hourly_heatmap returns hour-grouped counts."""
    for hour in range(3):
        entry = ScreenshotEntry(
            timestamp=datetime(2026, 5, 20, 10 + hour, 0, 0),
            screenshot_path=f"/tmp/heat_{hour}.jpg",
            analyzed=False,
        )
        aid = db.insert_activity(entry)
        analysis = ActivityRecord(
            app_name="Code", activity_category="coding", activity_summary="test"
        )
        db.update_activity_analysis(aid, analysis)

    heatmap = db.get_hourly_heatmap("2026-05-20", "2026-05-20")
    assert isinstance(heatmap, list)
    assert len(heatmap) == 3
    for row in heatmap:
        assert "hour" in row
        assert "cnt" in row


def test_delete_before(db):
    """delete_before removes entries before a cutoff date."""
    # Insert old and new entries
    for day in [15, 20]:
        entry = ScreenshotEntry(
            timestamp=datetime(2026, 5, day, 10, 0, 0),
            screenshot_path=f"/tmp/old_{day}.jpg",
            analyzed=False,
        )
        db.insert_activity(entry)

    deleted = db.delete_before("2026-05-18")
    assert deleted == 1  # Only the 15th should be deleted

    remaining = db.get_activities_by_date("2026-05-20")
    assert len(remaining) == 1


def test_cleanup_old_data_zero_retention(db):
    """cleanup_old_data with retention_days=0 does nothing."""
    result = db.cleanup_old_data(0)
    assert result == {"activities": 0, "meetings": 0}


def test_get_stats_with_categories(db):
    """get_stats returns category breakdown and app counts."""
    categories = ["coding", "coding", "browsing", "communication"]
    apps = ["VS Code", "VS Code", "Chrome", "Slack"]
    for i, (cat, app) in enumerate(zip(categories, apps)):
        entry = ScreenshotEntry(
            timestamp=datetime(2026, 5, 20, 10 + i, 0, 0),
            screenshot_path=f"/tmp/stat2_{i}.jpg",
            analyzed=False,
        )
        aid = db.insert_activity(entry)
        db.update_activity_analysis(aid, ActivityRecord(
            app_name=app, activity_category=cat, activity_summary="test"
        ))

    stats = db.get_stats("2026-05-20", "2026-05-20")
    assert stats["total_activities"] == 4
    assert stats["category_breakdown"]["coding"] == 2
    assert stats["top_apps"]["VS Code"] == 2
    assert stats["meetings_count"] == 0
