"""Tests for API endpoints — uses httpx AsyncClient with the FastAPI app."""

import pytest
from datetime import datetime

from screenmind.storage.models import ScreenshotEntry, ActivityRecord
import screenmind.api.dependencies as deps


@pytest.mark.asyncio
async def test_status_endpoint(client):
    resp = await client.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "running"


@pytest.mark.asyncio
async def test_timeline_empty(client):
    resp = await client.get("/api/timeline?date=2099-01-01")
    assert resp.status_code == 200
    data = resp.json()
    assert data["activities"] == []


@pytest.mark.asyncio
async def test_timeline_with_data(client, db):
    entry = ScreenshotEntry(
        timestamp=datetime(2026, 5, 16, 10, 0, 0),
        screenshot_path="/tmp/test.jpg",
        window_title="Test Window",
        analyzed=False,
    )
    db.insert_activity(entry)

    resp = await client.get("/api/timeline?date=2026-05-16")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["activities"]) >= 1
    assert any(a["window_title"] == "Test Window" for a in data["activities"])




@pytest.mark.asyncio
async def test_stats_endpoint(client, db):
    entry = ScreenshotEntry(
        timestamp=datetime(2026, 5, 16, 9, 0, 0),
        screenshot_path="/tmp/s.jpg",
        analyzed=False,
    )
    aid = db.insert_activity(entry)
    db.update_activity_analysis(aid, ActivityRecord(
        app_name="Code", activity_category="coding", activity_summary="test"
    ))

    resp = await client.get("/api/stats?date_from=2026-05-16&date_to=2026-05-16")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_activities" in data


@pytest.mark.asyncio
async def test_settings_get(client):
    resp = await client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert "capture_interval" in data
    assert "performance_mode" in data


@pytest.mark.asyncio
async def test_capture_pause_resume(client):
    resp = await client.post("/api/capture/pause")
    assert resp.status_code == 200
    assert resp.json()["paused"] is True

    resp = await client.post("/api/capture/resume")
    assert resp.status_code == 200
    assert resp.json()["paused"] is False




@pytest.mark.asyncio
async def test_auth_status(client):
    resp = await client.get("/api/auth/status")
    assert resp.status_code == 200
    assert "first_run" in resp.json()


@pytest.mark.asyncio
async def test_activity_not_found(client):
    resp = await client.get("/api/activity/99999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_clear_timeline(client, db):
    entry = ScreenshotEntry(
        timestamp=datetime(2026, 5, 20, 10, 0, 0),
        screenshot_path="/tmp/clear.jpg",
        analyzed=False,
    )
    db.insert_activity(entry)

    resp = await client.delete("/api/timeline/clear?date=2026-05-20")
    assert resp.status_code == 200
    data = resp.json()
    assert data["deleted"] >= 1


@pytest.mark.asyncio
async def test_meetings_empty(client):
    resp = await client.get("/api/meetings?date=2099-01-01")
    assert resp.status_code == 200
    assert resp.json()["meetings"] == []


@pytest.mark.asyncio
async def test_models_list(client):
    resp = await client.get("/api/models")
    assert resp.status_code == 200
    data = resp.json()
    assert "models" in data
    assert len(data["models"]) >= 1


@pytest.mark.asyncio
async def test_search_is_keyword_only(client, db):
    def add(summary, category):
        aid = db.insert_activity(ScreenshotEntry(
            timestamp=datetime(2026, 5, 16, 10, 0, 0),
            screenshot_path="/tmp/q.jpg",
            analyzed=False,
        ))
        db.update_activity_analysis(aid, ActivityRecord(
            app_name="Code", activity_category=category, activity_summary=summary
        ))
        return aid

    coding = add("Fixing the zebrafish parser", "coding")
    add("Reading about zebrafish", "browsing")

    resp = await client.get("/api/search", params={"q": "zebrafish"})
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) == 2
    assert {r["match_type"] for r in results} == {"keyword"}

    resp = await client.get("/api/search", params={"q": "zebrafish", "category": "coding"})
    assert [r["id"] for r in resp.json()["results"]] == [coding]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [
    ("GET", "/api/bookmarks"),
    ("GET", "/api/rewind"),
    ("GET", "/api/summary"),
    ("POST", "/api/chat"),
    ("GET", "/api/memos"),
    ("GET", "/api/agents"),
    ("POST", "/api/capture/bookmark"),
    ("POST", "/api/integrations/test"),
])
async def test_removed_routes_are_gone(client, method, path):
    resp = await client.request(method, path)
    assert resp.status_code in (404, 405)


@pytest.mark.asyncio
async def test_settings_hide_removed_features(client):
    data = (await client.get("/api/settings")).json()
    for key in ("bookmark_hotkey", "webhook_url", "notion_token", "agents_enabled", "auto_bookmark"):
        assert key not in data
