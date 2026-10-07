"""
How the export groups a day's activities into sessions.

A "session" is a pluggable idea. Today it is a stretch of activity with no
long idle gap. Other rules (by app, by calendar event) can be added as new
splitters without touching the archive writer. The splitter name and its
parameters go into the manifest, so a reader knows which rule made the
sessions.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Protocol


@dataclass
class Session:
    """A group of activities, oldest first. `index` starts at 1 per day."""

    index: int
    activities: List[Dict[str, Any]]
    meetings: List[Dict[str, Any]] = field(default_factory=list)
    ui_events: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def start(self) -> datetime:
        return self.activities[0]["_ts"]

    @property
    def end(self) -> datetime:
        return self.activities[-1]["_ts"]

    @property
    def session_id(self) -> str:
        """Stable within one export: date + index, e.g. "2026-10-06#03"."""
        return f"{self.start.date().isoformat()}#{self.index:02d}"


class SessionSplitter(Protocol):
    """Turns one day's activities (oldest first) into groups."""

    name: str

    def params(self) -> Dict[str, Any]:
        ...

    def split(self, activities: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        ...


class IdleGapSplitter:
    """A new session starts when no capture happens for `gap_minutes` or more.

    Capture runs every few seconds while the user works, so a long gap
    means the user was away, locked the screen or paused capture.
    """

    name = "idle_gap"

    def __init__(self, gap_minutes: float = 10):
        if gap_minutes <= 0:
            raise ValueError("gap_minutes must be positive")
        self.gap = timedelta(minutes=gap_minutes)
        self.gap_minutes = gap_minutes

    def params(self) -> Dict[str, Any]:
        return {"gap_minutes": self.gap_minutes}

    def split(self, activities: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        groups: List[List[Dict[str, Any]]] = []
        for act in activities:
            if groups and act["_ts"] - groups[-1][-1]["_ts"] < self.gap:
                groups[-1].append(act)
            else:
                groups.append([act])
        return groups


SPLITTERS = {IdleGapSplitter.name: IdleGapSplitter}


def build_sessions(
    activities: List[Dict[str, Any]],
    splitter: SessionSplitter,
    meetings: List[Dict[str, Any]] = (),
    ui_events: List[Dict[str, Any]] = (),
) -> List[Session]:
    """Split activities and attach meetings and UI events to sessions.

    A meeting goes to every session it overlaps. A UI event goes to the
    session of its linked activity, or else to the session whose time span
    holds it. Items that fit no session stay only in the day files.
    """
    sessions = [Session(i, group) for i, group in enumerate(splitter.split(activities), start=1)]

    by_activity = {a["id"]: s for s in sessions for a in s.activities}
    for ev in ui_events:
        s = by_activity.get(ev.get("activity_id"))
        if s is None:
            s = next((s for s in sessions if s.start <= ev["_ts"] <= s.end), None)
        if s is not None:
            s.ui_events.append(ev)
            ev["_session_id"] = s.session_id

    for m in meetings:
        m_end = m.get("_end") or m["_ts"]
        for s in sessions:
            if m["_ts"] <= s.end and m_end >= s.start:
                s.meetings.append(m)
                m.setdefault("_session_ids", []).append(s.session_id)

    return sessions
