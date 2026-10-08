"""
Call detection rules.

Decides whether a call is running from what is on screen right now:
every visible window (owner app + title, on any display) and, where the OS
tells us, which apps are using the microphone.

A plain "app is open" check is not enough. Slack and Chrome are open all
day, so each known call app has a rule that needs a call-specific window
title or the app holding the mic.
"""

import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Set

# Window owners that are web browsers (macOS owner names, Windows exe names).
BROWSER_OWNERS = {
    "google chrome", "google chrome canary", "chromium", "safari", "safari technology preview",
    "arc", "brave browser", "microsoft edge", "vivaldi", "opera", "orion", "firefox", "zen",
    "chrome", "msedge", "brave",
}

# Google Meet tab title during a call: "Meet - Daily sync" (Windows adds " - Google Chrome").
_MEET_TITLE = re.compile(r"^meet\s*[-–—]\s*\S")
_ZOOM_TITLE = re.compile(r"\bzoom (meeting|webinar)\b")
_TEAMS_TITLE = re.compile(r"\b(meeting|call)\b")
_WEBEX_TITLE = re.compile(r"\bmeeting\b|personal room")


@dataclass
class CallMatch:
    """One running call, as seen on screen."""

    app: str                      # label stored in meetings.app_name ("Google Meet", "Slack")
    owner: Optional[str] = None   # window owner or mic app, used for mic keep-alive
    title: Optional[str] = None   # call window title, when there is a window
    pid: Optional[int] = None
    bounds: Optional[tuple] = None
    is_browser: bool = False


def _lower(s: Optional[str]) -> str:
    return (s or "").lower()


def _rule(keyword: str, owner: str, title: str, owner_on_mic: bool) -> Optional[str]:
    """App label if this window is a call for a built-in keyword, else None.
    Returns "" for a known keyword whose rule did not match."""
    if keyword == "meet":
        in_browser = owner in BROWSER_OWNERS or "meet" in owner  # "Google Meet" web app
        return "Google Meet" if in_browser and _MEET_TITLE.search(title) else ""
    if keyword == "zoom":
        if _ZOOM_TITLE.search(title) or ("zoom" in owner and owner_on_mic):
            return "Zoom"
        return ""
    if keyword == "teams":
        if ("teams" in owner or "teams" in title) and (_TEAMS_TITLE.search(title) or owner_on_mic):
            return "Microsoft Teams"
        return ""
    if keyword == "webex":
        if ("webex" in owner or "webex" in title) and (_WEBEX_TITLE.search(title) or owner_on_mic):
            return "Webex"
        return ""
    if keyword == "slack":
        if "slack" in owner and ("huddle" in title or owner_on_mic):
            return "Slack"
        return ""
    if keyword == "discord":
        return "Discord" if "discord" in owner and owner_on_mic else ""
    return None


# Native call apps: holding the mic is enough, even with no visible window
# (Slack huddle while the Slack window is on another Space).
_MIC_ONLY_APPS = {"zoom": "Zoom", "teams": "Microsoft Teams", "webex": "Webex",
                  "slack": "Slack", "discord": "Discord"}


def match_call(windows: Iterable[dict], mic_apps: Optional[Set[str]],
               keywords: List[str]) -> Optional[CallMatch]:
    """Find a running call.

    Args:
        windows: visible windows, front to back, as {"owner", "title", "pid", "bounds"}.
        mic_apps: lowercased names of apps capturing the mic, or None if unknown.
        keywords: settings.meeting_apps_list. Built-in keywords use the rules
            above; any other keyword matches a window whose owner or title
            contains it.
    """
    mic = mic_apps or set()
    windows = list(windows)
    for win in windows:
        owner = _lower(win.get("owner"))
        title = _lower(win.get("title"))
        if not owner and not title:
            continue
        for kw in keywords:
            label = _rule(kw, owner, title, owner in mic)
            if label is None:  # user-added keyword
                label = win.get("owner") if (kw in owner or kw in title) else ""
            if label:
                return CallMatch(
                    app=label, owner=win.get("owner"), title=win.get("title"),
                    pid=win.get("pid"), bounds=win.get("bounds"),
                    is_browser=owner in BROWSER_OWNERS,
                )
    for kw in keywords:
        label = _MIC_ONLY_APPS.get(kw)
        if not label:
            continue
        for app in sorted(mic):
            if kw in app:
                return CallMatch(app=label, owner=app)
    return None


def owner_on_mic(owner: Optional[str], mic_apps: Optional[Set[str]]) -> bool:
    """Whether this app is still capturing the mic (call still running)."""
    return bool(owner) and bool(mic_apps) and owner.lower() in mic_apps


# Bits of a call window title that come and go while the call stays the
# same: Meet's speaking icon (🔊), Slack's unread marker ("* "), unread count
# (" - 2 new items") and window tag (" [Main]").
_TITLE_NOISE = (
    re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]"),
    re.compile(r"\s*[-–—]\s*\d+\s+new\s+items?\b", re.IGNORECASE),
    re.compile(r"^\s*\*\s*"),
    re.compile(r"\s*\[main\]\s*$", re.IGNORECASE),
)


def normalize_title(title: Optional[str]) -> str:
    """Call window title without the markers that change during a call."""
    t = title or ""
    for pattern in _TITLE_NOISE:
        t = pattern.sub("", t)
    return " ".join(t.split()).lower()


def same_call(app: str, url: Optional[str], title: Optional[str], is_browser: bool,
              row: dict) -> bool:
    """Whether a call seen now is the call in this meetings row.

    The room URL decides when both are known (Meet room). Otherwise a
    browser call compares titles, which name the meeting there ("Meet -
    Daily sync"). A native app's window title follows whatever the user has
    open (Slack channel), so the app alone decides."""
    if row.get("app_name") != app:
        return False
    if url and row.get("url"):
        return url == row["url"]
    if is_browser and title and row.get("window_title"):
        return normalize_title(title) == normalize_title(row["window_title"])
    return True
