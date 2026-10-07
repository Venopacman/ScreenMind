"""Tests for storage/models.py — Pydantic data models."""

from screenmind.storage.models import ActivityRecord, ScreenshotEntry


def test_activity_record_defaults():
    record = ActivityRecord()
    assert record.app_name == "unknown"
    assert record.activity_category == "other"
    assert record.mood == "neutral"
    assert record.confidence == 0.5
    assert record.visible_text_snippets == []


def test_activity_record_custom():
    record = ActivityRecord(
        app_name="VS Code",
        activity_category="coding",
        activity_summary="Editing main.py",
        mood="productive",
        confidence=0.9,
    )
    assert record.app_name == "VS Code"
    assert record.activity_category == "coding"
    assert record.confidence == 0.9


def test_screenshot_entry():
    from datetime import datetime
    entry = ScreenshotEntry(
        timestamp=datetime.now(),
        screenshot_path="/tmp/test.jpg",
        window_title="VS Code - main.py",
    )
    assert entry.analyzed is False
    assert entry.analysis is None
