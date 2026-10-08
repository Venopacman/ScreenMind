"""
Export captured data as a zip archive: one folder per day, sessions inside.

The format is documented in docs/export-format.md. Change SCHEMA_VERSION
when a field changes meaning or goes away.

Privacy: only activities with status 'ok' are exported. URLs and text are
cleaned again on the way out, because older rows were stored before the
current filters existed.
"""

import functools
import json
import logging
import re
import subprocess
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import IO, Any, Dict, Iterable, List, Optional

from screenmind import __version__
from screenmind.config import settings
from screenmind.export.sessions import Session, SessionSplitter, build_sessions
from screenmind.privacy.data_filter import DEFAULT_TYPES, filter_sensitive_text, parse_enabled_types
from screenmind.privacy.encryption import decrypt_image_bytes, is_encrypted
from screenmind.privacy.url_filter import sanitize_url

logger = logging.getLogger("screenmind.export.archive")

SCHEMA_VERSION = 2
MAX_DAYS = 31
# The workflows mapper takes uploads up to 1 MB. Leave some room.
MAX_TEXT_FILE_BYTES = 900_000
# Screen text per activity in the session .md files. Full text is in activities.jsonl.
SCREEN_TEXT_MAX_CHARS = 4000
MEETING_TRANSCRIPT_MAX_CHARS = 20_000
# UI events with no frame of their app, per gap between two frames in the .md
# files. All of them are in ui_events.jsonl.
UNLINKED_ACTIONS_MAX_LINES = 50

# Always applied on export, on top of what the user turned on in settings.
_DEFAULT_FILTER_TYPES = list(DEFAULT_TYPES)

_ACTIVITY_COLUMNS = """
    a.id, a.timestamp, a.screenshot_path, a.window_title, a.detected_app,
    a.app_name, a.category, a.summary, a.details, a.visible_text,
    a.mood, a.confidence, a.ocr_text, a.scene_description, a.organized_text,
    a.analysis_method, a.active_url, a.user_actions
"""


# ── Helpers ──────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=1)
def git_sha() -> Optional[str]:
    """Commit of the running code, or None for an installed package."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode != 0:
            return None
        return out.stdout.strip() or None
    except Exception:
        return None


def filter_types() -> List[str]:
    configured = parse_enabled_types(settings.sensitive_filter_types)
    return _DEFAULT_FILTER_TYPES + [t for t in configured if t not in _DEFAULT_FILTER_TYPES]


def _local(ts: str) -> datetime:
    """DB timestamps are naive local time. Attach the local offset (DST-aware)."""
    dt = datetime.fromisoformat(ts)
    return dt.astimezone() if dt.tzinfo is None else dt


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat(timespec="seconds") if dt else None


def time_zone_info() -> Dict[str, str]:
    now = datetime.now().astimezone()
    return {"name": now.tzname() or "", "utc_offset": now.strftime("%z")[:3] + ":" + now.strftime("%z")[3:]}


def user_slug(user: str) -> str:
    return re.sub(r"[^A-Za-z0-9._@-]+", "_", user.strip())[:60].strip("_") or "user"


def archive_name(user: str, date_from: date, date_to: date) -> str:
    span = date_from.isoformat() if date_from == date_to else f"{date_from}_{date_to}"
    return f"screenmind-export_{user_slug(user)}_{span}"


def _days(date_from: date, date_to: date) -> List[date]:
    return [date_from + timedelta(days=i) for i in range((date_to - date_from).days + 1)]


class _Scrubber:
    def __init__(self, types: List[str]):
        self.types = types

    def __call__(self, text: Optional[str]) -> Optional[str]:
        if not text:
            return text
        return filter_sensitive_text(text, self.types)["clean_text"]


# ── Loading one day ──────────────────────────────────────────────────

def _load_activities(db, day: date, scrub: _Scrubber) -> List[Dict[str, Any]]:
    rows = db._get_conn().execute(
        f"""
        SELECT {_ACTIVITY_COLUMNS}
        FROM activities a
        WHERE DATE(a.timestamp) = ? AND a.status = 'ok'
        ORDER BY a.timestamp, a.id
        """,
        (day.isoformat(),),
    ).fetchall()

    out = []
    for r in rows:
        r = dict(r)
        try:
            visible = json.loads(r["visible_text"]) if r["visible_text"] else []
        except (json.JSONDecodeError, TypeError):
            visible = []
        out.append({
            "id": r["id"],
            "_ts": _local(r["timestamp"]),
            "_screenshot_path": r["screenshot_path"],
            "app": r["app_name"] or r["detected_app"],
            "detected_app": r["detected_app"],
            "window_title": scrub(r["window_title"]),
            "url": sanitize_url(r["active_url"]),
            "category": r["category"],
            "summary": scrub(r["summary"]),
            "details": scrub(r["details"]),
            "scene_description": scrub(r["scene_description"]),
            "screen_text": scrub(r["ocr_text"]),
            "organized_text": scrub(r["organized_text"]),
            "visible_text": [scrub(v) for v in visible if isinstance(v, str)],
            "user_actions": scrub(r["user_actions"]),
            "mood": r["mood"],
            "confidence": r["confidence"],
            "analysis_method": r["analysis_method"],
        })
    return out


def _load_ui_events(db, day: date, scrub: _Scrubber) -> List[Dict[str, Any]]:
    # Events linked to a row that is not 'ok' are left out with that row.
    rows = db._get_conn().execute(
        """
        SELECT e.* FROM ui_events e
        LEFT JOIN activities a ON a.id = e.activity_id
        WHERE DATE(e.timestamp) = ? AND (e.activity_id IS NULL OR a.status = 'ok')
        ORDER BY e.timestamp, e.id
        """,
        (day.isoformat(),),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "_ts": _local(r["timestamp"]),
            "activity_id": r["activity_id"],
            "type": r["type"],
            "app": r["app_name"],
            "window_title": scrub(r["window_title"]),
            "url": sanitize_url(r["url"]),
            "element_role": r["element_role"],
            "element_name": scrub(r["element_name"]),
            "element_value": scrub(r["element_value"]),
            "text": scrub(r["text"]),
            "x": r["x"],
            "y": r["y"],
        }
        for r in rows
    ]


def _load_meetings(db, day: date, scrub: _Scrubber) -> List[Dict[str, Any]]:
    rows = db._get_conn().execute(
        "SELECT * FROM meetings WHERE DATE(start_time) = ? ORDER BY start_time, id",
        (day.isoformat(),),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "_ts": _local(r["start_time"]),
            "_end": _local(r["end_time"]) if r["end_time"] else None,
            "app": r["app_name"],
            "window_title": scrub(r["window_title"]),
            "url": sanitize_url(r["url"]),
            "duration_minutes": r["duration_minutes"],
            "summary": scrub(r["summary"]),
            "transcript": scrub(r["transcript"]),
        }
        for r in rows
    ]


# ── Rendering ────────────────────────────────────────────────────────

def _public(item: Dict[str, Any], **extra) -> Dict[str, Any]:
    """Drop internal "_" keys and add the fields that need formatting."""
    return {**{k: v for k, v in item.items() if not k.startswith("_")}, **extra}


def _activity_json(a: Dict[str, Any]) -> Dict[str, Any]:
    return _public(
        a, timestamp=_iso(a["_ts"]), session_id=a.get("_session_id"),
        screenshot=a.get("_screenshot_arcname"),
    )


def _meeting_json(m: Dict[str, Any]) -> Dict[str, Any]:
    return _public(
        m, start=_iso(m["_ts"]), end=_iso(m.get("_end")),
        session_ids=m.get("_session_ids", []),
    )


def _event_json(e: Dict[str, Any]) -> Dict[str, Any]:
    return _public(e, timestamp=_iso(e["_ts"]), session_id=e.get("_session_id"))


def _fenced(text: str) -> str:
    """A code block whose fence is longer than any backtick run in the text."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


def _trim(text: str, limit: int, where: str) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... {len(text) - limit} more chars, full text in {where}]"


def _hm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def session_file_stem(s: Session) -> str:
    return f"{s.index:02d}_{s.start.strftime('%H%M')}-{s.end.strftime('%H%M')}"


def _activity_block(a: Dict[str, Any], prev_text: Optional[str]) -> str:
    lines = [f"### {a['_ts'].strftime('%H:%M:%S')} | {a['app'] or 'unknown app'} | {a['category'] or 'uncategorized'}"]
    if a["window_title"]:
        lines.append(f"Window: {a['window_title']}")
    if a["url"]:
        lines.append(f"URL: {a['url']}")
    if a["summary"]:
        lines.append(f"Summary: {a['summary']}")
    if a["details"]:
        lines.append(f"Details: {a['details']}")
    if a["user_actions"]:
        lines.append(f"User actions: {a['user_actions']}")
    if a.get("_screenshot_arcname"):
        lines.append(f"Screenshot: {a['_screenshot_arcname']}")
    text = a["organized_text"] or a["screen_text"]
    if text:
        if text == prev_text:
            lines.append("Screen text: same as above.")
        else:
            lines.append("Screen text:")
            lines.append(_fenced(_trim(text, SCREEN_TEXT_MAX_CHARS, "activities.jsonl")))
    return "\n".join(lines) + "\n"


def _unlinked_block(events: List[Dict[str, Any]]) -> Optional[str]:
    """UI events that no screenshot of their app followed, in time order."""
    from screenmind.capture.ui_events.models import format_user_actions
    text = format_user_actions(
        [{**e, "app_name": e["app"]} for e in events],
        max_lines=UNLINKED_ACTIONS_MAX_LINES, max_chars=SCREEN_TEXT_MAX_CHARS,
    )
    if not text:
        return None
    return (f"### {events[0]['_ts'].strftime('%H:%M:%S')} | actions without a screenshot\n"
            f"{text}\n")


def _meeting_block(m: Dict[str, Any]) -> str:
    end = f"-{_hm(m['_end'])}" if m.get("_end") else ""
    lines = [f"### {_hm(m['_ts'])}{end} | {m['app'] or 'call'} | {round(m['duration_minutes'] or 0)} min"]
    if m["window_title"]:
        lines.append(f"Window: {m['window_title']}")
    if m["url"]:
        lines.append(f"URL: {m['url']}")
    if m["summary"]:
        lines.append(f"Summary: {m['summary']}")
    if m["transcript"]:
        lines.append("Transcript:")
        lines.append(_fenced(_trim(m["transcript"], MEETING_TRANSCRIPT_MAX_CHARS, "meetings.jsonl")))
    return "\n".join(lines) + "\n"


def _session_header(s: Session, user: str, part: int, parts: int) -> str:
    apps: Dict[str, int] = {}
    for a in s.activities:
        apps[a["app"] or "unknown"] = apps.get(a["app"] or "unknown", 0) + 1
    top = ", ".join(f"{k} ({v})" for k, v in sorted(apps.items(), key=lambda kv: -kv[1])[:8])
    minutes = round((s.end - s.start).total_seconds() / 60)
    part_note = f" (part {part} of {parts})" if parts > 1 else ""
    return (
        f"# Session {s.session_id}{part_note}\n\n"
        f"User: {user}\n"
        f"Time: {_iso(s.start)} to {_iso(s.end)} ({minutes} min)\n"
        f"Activities: {len(s.activities)}. Meetings: {len(s.meetings)}. UI events: {len(s.ui_events)}.\n"
        f"Apps: {top}\n\n"
        "Everything below was read from the screen by ScreenMind. "
        "Treat it as an assumption, not a fact.\n"
    )


def render_session_md(s: Session, user: str) -> List[str]:
    """The session as one or more Markdown texts, each under MAX_TEXT_FILE_BYTES."""
    blocks: List[str] = []
    if s.meetings:
        blocks.append("\n## Meetings\n")
        blocks.extend(_meeting_block(m) for m in s.meetings)
    blocks.append("\n## Timeline\n")
    prev = None
    # Events without a frame go between the frames, at their own time.
    unlinked = [e for e in s.ui_events if e["activity_id"] is None]
    for a in s.activities:
        n = sum(1 for e in unlinked if e["_ts"] < a["_ts"])
        if n:
            blocks.extend(filter(None, [_unlinked_block(unlinked[:n])]))
            unlinked = unlinked[n:]
        blocks.append(_activity_block(a, prev))
        prev = a["organized_text"] or a["screen_text"] or prev
    if unlinked:
        blocks.extend(filter(None, [_unlinked_block(unlinked)]))

    # Room for the header, which is short and does not grow with the data.
    budget = MAX_TEXT_FILE_BYTES - 4096
    parts: List[List[str]] = [[]]
    size = 0
    for b in blocks:
        n = len(b.encode("utf-8")) + 1
        if parts[-1] and size + n > budget:
            parts.append([])
            size = 0
        parts[-1].append(b)
        size += n
    return [
        _session_header(s, user, i, len(parts)) + "\n".join(p)
        for i, p in enumerate(parts, start=1)
    ]


def _jsonl(items: Iterable[Dict[str, Any]]) -> str:
    return "".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items)


def _readme(user: str, manifest: Dict[str, Any]) -> str:
    return (
        "# ScreenMind export\n\n"
        f"User: {user}\n"
        f"Dates: {manifest['date_from']} to {manifest['date_to']}\n\n"
        "One folder per day. In each day folder:\n\n"
        "- `sessions/*.md`: one text file per session, for people and LLMs. Each file is under 1 MB.\n"
        "- `sessions.json`: the list of sessions with times, counts and file names.\n"
        "- `activities.jsonl`: one line per captured screen, full text.\n"
        "- `ui_events.jsonl`: clicks, typing and app switches.\n"
        "- `meetings.jsonl`: recorded calls.\n"
        "- `screenshots/`: the screen images, if included.\n\n"
        "`manifest.json` has the format version, the session rule and the counts.\n"
        "Full format: docs/export-format.md in the ScreenMind repo.\n"
    )


# ── Public API ───────────────────────────────────────────────────────

def _screenshot_source(path: Optional[str], root: Path) -> Optional[Path]:
    """The screenshot file, only if it exists inside the screenshots dir."""
    if not path:
        return None
    try:
        p = Path(path).resolve()
        p.relative_to(root)
    except (ValueError, OSError):
        return None
    return p if p.is_file() else None


def collect_day(db, day: date, splitter: SessionSplitter, scrub: _Scrubber):
    activities = _load_activities(db, day, scrub)
    meetings = _load_meetings(db, day, scrub)
    events = _load_ui_events(db, day, scrub)
    sessions = build_sessions(activities, splitter, meetings, events)
    for s in sessions:
        for a in s.activities:
            a["_session_id"] = s.session_id
    return activities, meetings, events, sessions


def summarize(db, date_from: date, date_to: date, splitter: SessionSplitter) -> Dict[str, Any]:
    """Counts and screenshot size per day, without building the archive."""
    scrub = _Scrubber(filter_types())
    root = settings.screenshots_dir.resolve()
    days = []
    for day in _days(date_from, date_to):
        activities, meetings, events, sessions = collect_day(db, day, splitter, scrub)
        files = [_screenshot_source(a["_screenshot_path"], root) for a in activities]
        days.append({
            "date": day.isoformat(),
            "sessions": len(sessions),
            "activities": len(activities),
            "meetings": len(meetings),
            "ui_events": len(events),
            "screenshots": sum(1 for f in files if f),
            "screenshot_bytes": sum(f.stat().st_size for f in files if f),
        })
    return {"date_from": date_from.isoformat(), "date_to": date_to.isoformat(), "days": days}


def write_export(
    db,
    out: IO[bytes],
    user: str,
    date_from: date,
    date_to: date,
    splitter: SessionSplitter,
    include_screenshots: bool = True,
) -> Dict[str, Any]:
    """Write the zip archive to `out` and return its manifest."""
    if date_to < date_from:
        raise ValueError("date_to is before date_from")
    if (date_to - date_from).days + 1 > MAX_DAYS:
        raise ValueError(f"at most {MAX_DAYS} days per export")

    types = filter_types()
    scrub = _Scrubber(types)
    shots_root = settings.screenshots_dir.resolve()
    base = archive_name(user, date_from, date_to)
    manifest: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generator": "ScreenMind",
        "app_version": __version__,
        "git_sha": git_sha(),
        "exported_at": _iso(datetime.now().astimezone()),
        "user": user,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "time_zone": time_zone_info(),
        "session_rule": {"name": splitter.name, "params": splitter.params()},
        "includes_screenshots": include_screenshots,
        "privacy": {
            "activity_status": "ok",
            "url_filter": "screenmind.privacy.url_filter.sanitize_url",
            "text_filter_types": types,
            "screenshots_redacted": False,
        },
        "limits": {
            "text_file_max_bytes": MAX_TEXT_FILE_BYTES,
            "screen_text_max_chars_in_md": SCREEN_TEXT_MAX_CHARS,
            "meeting_transcript_max_chars_in_md": MEETING_TRANSCRIPT_MAX_CHARS,
        },
        "days": [],
    }

    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for day in _days(date_from, date_to):
            activities, meetings, events, sessions = collect_day(db, day, splitter, scrub)
            info = {
                "date": day.isoformat(), "sessions": len(sessions),
                "activities": len(activities), "meetings": len(meetings),
                "ui_events": len(events), "screenshots": 0, "screenshots_missing": 0,
            }
            manifest["days"].append(info)
            if not activities and not meetings:
                continue
            folder = f"{base}/{day.isoformat()}"

            if include_screenshots:
                for a in activities:
                    src = _screenshot_source(a["_screenshot_path"], shots_root)
                    encrypted = src is not None and is_encrypted(src)
                    data = decrypt_image_bytes(src) if encrypted else None
                    if src is None or (encrypted and data is None):
                        info["screenshots_missing"] += 1
                        continue
                    # JPEGs are already compressed: store them as they are.
                    arc = f"screenshots/{a['id']}_{src.name}"
                    if encrypted:
                        zf.writestr(f"{folder}/{arc}", data, compress_type=zipfile.ZIP_STORED)
                    else:
                        zf.write(src, f"{folder}/{arc}", compress_type=zipfile.ZIP_STORED)
                    a["_screenshot_arcname"] = arc
                    info["screenshots"] += 1

            session_list = []
            for s in sessions:
                stem = session_file_stem(s)
                texts = render_session_md(s, user)
                names = [
                    f"sessions/{stem}.md" if len(texts) == 1 else f"sessions/{stem}_part{i}.md"
                    for i in range(1, len(texts) + 1)
                ]
                for name, text in zip(names, texts):
                    zf.writestr(f"{folder}/{name}", text)
                session_list.append({
                    "session_id": s.session_id,
                    "start": _iso(s.start),
                    "end": _iso(s.end),
                    "activities": len(s.activities),
                    "meetings": len(s.meetings),
                    "ui_events": len(s.ui_events),
                    "apps": sorted({a["app"] for a in s.activities if a["app"]}),
                    "files": names,
                })

            zf.writestr(f"{folder}/sessions.json", json.dumps(session_list, ensure_ascii=False, indent=2))
            zf.writestr(f"{folder}/activities.jsonl", _jsonl(_activity_json(a) for a in activities))
            zf.writestr(f"{folder}/ui_events.jsonl", _jsonl(_event_json(e) for e in events))
            zf.writestr(f"{folder}/meetings.jsonl", _jsonl(_meeting_json(m) for m in meetings))

        manifest["totals"] = {
            k: sum(d[k] for d in manifest["days"])
            for k in ("sessions", "activities", "meetings", "ui_events", "screenshots", "screenshots_missing")
        }
        zf.writestr(f"{base}/README.md", _readme(user, manifest))
        zf.writestr(f"{base}/manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    logger.info(
        "Exported %s day(s): %s session(s), %s activities, %s screenshot(s)",
        len(manifest["days"]), manifest["totals"]["sessions"],
        manifest["totals"]["activities"], manifest["totals"]["screenshots"],
    )
    return manifest
