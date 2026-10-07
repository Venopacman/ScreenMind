"""Tests for the data export: session splitting, the zip archive and the API route."""

import io
import json
import zipfile
from datetime import date, datetime, timedelta

import pytest

from screenmind.config import settings
from screenmind.export import archive
from screenmind.export.archive import write_export
from screenmind.export.sessions import IdleGapSplitter, build_sessions

DAY = date(2026, 10, 6)
JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"


def _ts(h, m, s=0):
    return datetime(2026, 10, 6, h, m, s)


def _add_activity(db, ts, status="ok", **fields):
    row = {
        "timestamp": ts.isoformat(), "screenshot_path": fields.pop("screenshot_path", "/nowhere.jpg"),
        "status": status, "app_name": "Chrome", "category": "browsing",
        "summary": "Reads docs", "window_title": "Docs", **fields,
    }
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn = db._get_conn()
    cur = conn.execute(f"INSERT INTO activities ({cols}) VALUES ({marks})", list(row.values()))
    conn.commit()
    return cur.lastrowid


def _add_meeting(db, start, end, **fields):
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO meetings (start_time, end_time, app_name, duration_minutes, transcript, summary, url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (start.isoformat(), end.isoformat(), "Google Meet",
         (end - start).total_seconds() / 60, fields.get("transcript", ""),
         fields.get("summary", ""), fields.get("url")),
    )
    conn.commit()


def _add_ui_event(db, ts, activity_id=None, **fields):
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO ui_events (timestamp, type, app_name, text, url, activity_id) VALUES (?, ?, ?, ?, ?, ?)",
        (ts.isoformat(), fields.get("type", "text"), "Chrome", fields.get("text"), fields.get("url"), activity_id),
    )
    conn.commit()


def _export(db, **kw):
    buf = io.BytesIO()
    manifest = write_export(
        db, buf, kw.pop("user", "pavel@example.com"), kw.pop("date_from", DAY), kw.pop("date_to", DAY),
        IdleGapSplitter(kw.pop("gap", 10)), kw.pop("screenshots", True),
    )
    buf.seek(0)
    zf = zipfile.ZipFile(buf)
    root = zf.namelist()[0].split("/")[0]
    return manifest, zf, root


def _jsonl(zf, name):
    return [json.loads(line) for line in zf.read(name).decode().splitlines()]


# ── Session splitting ────────────────────────────────────────────────

def _acts(*times):
    return [{"id": i, "_ts": t} for i, t in enumerate(times, start=1)]


def test_idle_gap_splits_on_long_gaps_only():
    acts = _acts(_ts(9, 0), _ts(9, 5), _ts(9, 14, 59), _ts(9, 25), _ts(11, 0))
    groups = IdleGapSplitter(10).split(acts)
    assert [[a["id"] for a in g] for g in groups] == [[1, 2, 3], [4], [5]]


def test_idle_gap_empty_day_and_bad_gap():
    assert IdleGapSplitter(10).split([]) == []
    with pytest.raises(ValueError):
        IdleGapSplitter(0)


def test_build_sessions_attaches_meetings_and_events():
    acts = _acts(_ts(9, 0), _ts(9, 30), _ts(11, 0), _ts(11, 5))
    meeting = {"id": 1, "_ts": _ts(8, 59), "_end": _ts(11, 2)}  # overlaps all three sessions
    events = [
        {"id": 1, "_ts": _ts(11, 3), "activity_id": None},  # by time
        {"id": 2, "_ts": _ts(20, 0), "activity_id": 1},      # by link
        {"id": 3, "_ts": _ts(15, 0), "activity_id": None},   # no session
    ]
    sessions = build_sessions(acts, IdleGapSplitter(10), [meeting], events)
    assert len(sessions) == 3
    assert sessions[0].session_id == "2026-10-06#01"
    assert meeting["_session_ids"] == ["2026-10-06#01", "2026-10-06#02", "2026-10-06#03"]
    assert [e["id"] for e in sessions[0].ui_events] == [2]
    assert [e["id"] for e in sessions[2].ui_events] == [1]
    assert "_session_id" not in events[2]


# ── Archive ──────────────────────────────────────────────────────────

def test_export_layout_and_manifest(db):
    _add_activity(db, _ts(9, 0))
    _add_activity(db, _ts(9, 5))
    _add_activity(db, _ts(10, 0))
    manifest, zf, root = _export(db, screenshots=False)

    names = set(zf.namelist())
    day = f"{root}/2026-10-06"
    assert root == "screenmind-export_pavel@example.com_2026-10-06"
    assert {f"{root}/manifest.json", f"{root}/README.md", f"{day}/sessions.json",
            f"{day}/activities.jsonl", f"{day}/ui_events.jsonl", f"{day}/meetings.jsonl",
            f"{day}/sessions/01_0900-0905.md", f"{day}/sessions/02_1000-1000.md"} <= names

    m = json.loads(zf.read(f"{root}/manifest.json"))
    assert m == manifest
    assert m["schema_version"] == archive.SCHEMA_VERSION
    assert m["user"] == "pavel@example.com"
    assert m["session_rule"] == {"name": "idle_gap", "params": {"gap_minutes": 10}}
    assert m["includes_screenshots"] is False
    assert m["totals"]["sessions"] == 2 and m["totals"]["activities"] == 3
    assert m["time_zone"]["utc_offset"]

    sessions = json.loads(zf.read(f"{day}/sessions.json"))
    assert [s["files"] for s in sessions] == [["sessions/01_0900-0905.md"], ["sessions/02_1000-1000.md"]]
    acts = _jsonl(zf, f"{day}/activities.jsonl")
    assert [a["session_id"] for a in acts] == ["2026-10-06#01", "2026-10-06#01", "2026-10-06#02"]
    assert acts[0]["timestamp"].startswith("2026-10-06T09:00:00")
    assert not any(k.startswith("_") for a in acts for k in a)
    assert "ocr_boxes" not in acts[0] and "embedding" not in acts[0]


def test_export_only_ok_rows(db):
    ok = _add_activity(db, _ts(9, 0), summary="kept")
    skipped = _add_activity(db, _ts(9, 1), status="skipped", summary="skipped row")
    _add_activity(db, _ts(9, 2), status="failed", summary="failed row")
    _add_activity(db, _ts(9, 3), status="pending", summary="pending row")
    _add_ui_event(db, _ts(9, 0, 30), activity_id=ok, text="from ok")
    _add_ui_event(db, _ts(9, 1, 30), activity_id=skipped, text="from skipped")
    _add_ui_event(db, _ts(9, 2, 30), text="not linked yet")

    _, zf, root = _export(db, screenshots=False)
    day = f"{root}/2026-10-06"
    acts = _jsonl(zf, f"{day}/activities.jsonl")
    assert [a["summary"] for a in acts] == ["kept"]
    events = _jsonl(zf, f"{day}/ui_events.jsonl")
    assert [e["text"] for e in events] == ["from ok", "not linked yet"]
    md = zf.read(f"{day}/sessions/01_0900-0900.md").decode()
    assert "kept" in md and "skipped row" not in md


def test_export_cleans_urls_and_text_again(db):
    # Rows from before the URL filter existed hold raw tokens.
    _add_activity(
        db, _ts(9, 0),
        active_url="https://app.example.com/reset-password?token=abc123secret#frag",
        ocr_text=f"Authorization: Bearer {JWT}\npassword: hunter2!x",
        window_title="password: Sup3rSecret!",
    )
    _add_activity(db, _ts(9, 1), active_url="https://docs.example.com/guide?tab=api&q=private&sig=xyz")
    _add_ui_event(db, _ts(9, 0, 10), url="https://x.example.com/a?code=SECRET", text="pwd: Tr0ub4dor&3")
    _add_meeting(db, _ts(9, 0), _ts(9, 1), transcript="my password: letmein99", url="https://meet.example.com/abc?pwd=XYZ")

    _, zf, root = _export(db, screenshots=False)
    day = f"{root}/2026-10-06"
    everything = "".join(zf.read(n).decode() for n in zf.namelist() if not n.endswith(".jpg"))
    for secret in ("abc123secret", "frag", JWT, "hunter2!x", "Sup3rSecret!", "sig=xyz", "q=private",
                   "SECRET", "Tr0ub4dor", "letmein99", "pwd=XYZ"):
        assert secret not in everything, secret

    acts = _jsonl(zf, f"{day}/activities.jsonl")
    assert acts[0]["url"] == "https://app.example.com/"
    assert acts[1]["url"] == "https://docs.example.com/guide?tab=api"
    assert _jsonl(zf, f"{day}/meetings.jsonl")[0]["url"] == "https://meet.example.com/abc"


def test_export_screenshots(db):
    shots = settings.screenshots_dir / "2026-10-06"
    shots.mkdir(parents=True, exist_ok=True)
    good = shots / "09-00-00_000_m1.jpg"
    good.write_bytes(b"\xff\xd8fake-jpeg")
    a1 = _add_activity(db, _ts(9, 0), screenshot_path=str(good))
    _add_activity(db, _ts(9, 1), screenshot_path=str(shots / "missing.jpg"))
    _add_activity(db, _ts(9, 2), screenshot_path="/etc/hosts")  # outside the screenshots dir

    manifest, zf, root = _export(db, screenshots=True)
    day = f"{root}/2026-10-06"
    arc = f"screenshots/{a1}_09-00-00_000_m1.jpg"
    assert zf.read(f"{day}/{arc}") == b"\xff\xd8fake-jpeg"
    assert zf.getinfo(f"{day}/{arc}").compress_type == zipfile.ZIP_STORED
    assert [n for n in zf.namelist() if "/screenshots/" in n] == [f"{day}/{arc}"]
    assert manifest["days"][0]["screenshots"] == 1
    assert manifest["days"][0]["screenshots_missing"] == 2
    acts = _jsonl(zf, f"{day}/activities.jsonl")
    assert [a["screenshot"] for a in acts] == [arc, None, None]
    assert f"Screenshot: {arc}" in zf.read(f"{day}/sessions/01_0900-0902.md").decode()

    _, zf, _ = _export(db, screenshots=False)
    assert not [n for n in zf.namelist() if "/screenshots/" in n]


def test_session_md_is_split_under_size_limit(db, monkeypatch):
    monkeypatch.setattr(archive, "MAX_TEXT_FILE_BYTES", 20_000)
    for i in range(30):
        _add_activity(db, _ts(9, 0) + timedelta(seconds=10 * i), ocr_text=f"screen {i} " + "x" * 3000)

    _, zf, root = _export(db, screenshots=False)
    day = f"{root}/2026-10-06"
    files = json.loads(zf.read(f"{day}/sessions.json"))[0]["files"]
    assert len(files) > 1
    assert files[0] == "sessions/01_0900-0904_part1.md"
    for i, name in enumerate(files, start=1):
        text = zf.read(f"{day}/{name}")
        assert len(text) <= 20_000
        assert f"(part {i} of {len(files)})".encode() in text
    joined = b"".join(zf.read(f"{day}/{n}") for n in files).decode()
    assert all(f"screen {i} " in joined for i in range(30))


def test_session_md_trims_and_dedupes_screen_text(db):
    _add_activity(db, _ts(9, 0), ocr_text="a" * (archive.SCREEN_TEXT_MAX_CHARS + 50))
    _add_activity(db, _ts(9, 1), ocr_text="a" * (archive.SCREEN_TEXT_MAX_CHARS + 50))
    _add_activity(db, _ts(9, 2), ocr_text="has ``` fences")

    _, zf, root = _export(db, screenshots=False)
    md = zf.read(f"{root}/2026-10-06/sessions/01_0900-0902.md").decode()
    assert "[... 50 more chars, full text in activities.jsonl]" in md
    assert "Screen text: same as above." in md
    assert "````\nhas ``` fences\n````" in md


def test_export_range_and_limits(db):
    _add_activity(db, _ts(9, 0))
    manifest, zf, root = _export(db, date_from=DAY - timedelta(days=1), date_to=DAY + timedelta(days=1), screenshots=False)
    assert root.endswith("_2026-10-05_2026-10-07")
    assert [d["date"] for d in manifest["days"]] == ["2026-10-05", "2026-10-06", "2026-10-07"]
    # Empty days are listed in the manifest but get no folder.
    assert not [n for n in zf.namelist() if "/2026-10-05/" in n]

    with pytest.raises(ValueError):
        _export(db, date_from=DAY, date_to=DAY - timedelta(days=1))
    with pytest.raises(ValueError):
        _export(db, date_to=DAY + timedelta(days=archive.MAX_DAYS))


# ── API ──────────────────────────────────────────────────────────────

async def test_api_export_zip(client, db):
    _add_activity(db, _ts(9, 0))
    resp = await client.get("/api/export", params={"date": "2026-10-06", "user": "Pavel S.", "screenshots": "false"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert 'filename="screenmind-export_Pavel_S._2026-10-06.zip"' in resp.headers["content-disposition"]
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    manifest = json.loads(zf.read("screenmind-export_Pavel_S._2026-10-06/manifest.json"))
    assert manifest["user"] == "Pavel S."
    assert manifest["totals"]["activities"] == 1


async def test_api_export_range_and_gap(client, db):
    _add_activity(db, _ts(9, 0))
    _add_activity(db, _ts(9, 20))
    resp = await client.get("/api/export", params={
        "from": "2026-10-05", "to": "2026-10-06", "user": "u", "gap_minutes": 30, "screenshots": "false",
    })
    assert resp.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    manifest = json.loads(zf.read("screenmind-export_u_2026-10-05_2026-10-06/manifest.json"))
    assert manifest["totals"]["sessions"] == 1
    assert manifest["session_rule"]["params"] == {"gap_minutes": 30}


@pytest.mark.parametrize("params", [
    {"date": "2026-10-06"},                                   # no user
    {"date": "2026-10-06", "user": "   "},                    # blank user
    {"user": "u"},                                            # no date
    {"date": "06.10.2026", "user": "u"},                      # bad date
    {"from": "2026-10-06", "to": "2026-10-01", "user": "u"},  # reversed
    {"from": "2026-01-01", "to": "2026-12-31", "user": "u"},  # too long
])
async def test_api_export_bad_input(client, params):
    resp = await client.get("/api/export", params=params)
    assert resp.status_code in (400, 422)


async def test_api_export_preview(client, db):
    _add_activity(db, _ts(9, 0))
    _add_activity(db, _ts(11, 0))
    resp = await client.get("/api/export/preview", params={"date": "2026-10-06"})
    assert resp.status_code == 200
    day = resp.json()["days"][0]
    assert day["date"] == "2026-10-06"
    assert day["sessions"] == 2 and day["activities"] == 2


async def test_api_export_refuses_remote_clients(app):
    from httpx import ASGITransport, AsyncClient
    transport = ASGITransport(app=app, client=("192.168.1.20", 5000))
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/api/export", params={"date": "2026-10-06", "user": "u"})
        assert resp.status_code == 403
        resp = await c.get("/api/export/preview", params={"date": "2026-10-06"})
        assert resp.status_code == 403


async def test_api_export_needs_pin_session(app, db):
    from httpx import ASGITransport, AsyncClient
    settings.dashboard_pin_hash = "set"
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/export", params={"date": "2026-10-06", "user": "u"})
            assert resp.status_code == 401
    finally:
        settings.dashboard_pin_hash = ""


async def test_api_export_deletes_temp_file(client, db, tmp_path, monkeypatch):
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    _add_activity(db, _ts(9, 0))
    resp = await client.get("/api/export", params={"date": "2026-10-06", "user": "u", "screenshots": "false"})
    assert resp.status_code == 200
    assert not list(tmp_path.glob("screenmind-export-*"))
