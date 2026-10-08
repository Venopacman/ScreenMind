"""
Frame labels from rules
Names the app and picks an activity category for a frame from what the OS
reports: the app name, the window title and the browser URL. No model runs.

App name hierarchy (first non-empty wins):
  L0: OS app name, only where the platform adapter trusts it (macOS)
  L1: title " - " extraction ("main.py - Visual Studio Code")
  L2: OS process name, unless it is a generic wrapper (electron, java)
  L3: a short clean title ("Calculator")
  L4: the OS process name as it is, else "unknown"

Category:
  - browser windows: from the site (URL host, else title words), else "browsing"
  - known apps: KNOWN_APP_CATEGORIES
  - anything else: "other"
"""

import logging
import re
from typing import Optional
from urllib.parse import urlparse

from screenmind.platform_support import adapter as platform_adapter
from screenmind.storage.models import ActivityRecord

logger = logging.getLogger("screenmind.engine.labels")

VALID_CATEGORIES = {
    "coding", "browsing", "communication", "writing",
    "design", "media", "terminal", "meeting", "idle", "other",
}

# Process names that are generic wrappers/runtimes — they tell us nothing
# about the actual application.  When the OS returns one of these, we skip it
# and fall back to window-title extraction.
# See: https://github.com/ayushh0110/ScreenMind/issues/6
GENERIC_PROCESS_NAMES = frozenset({
    # Language runtimes
    "electron", "java", "javaw", "python", "pythonw", "python3",
    "node", "ruby", "perl", "dotnet",
    # OS-level wrappers
    "applicationframehost",   # Windows UWP (Calculator, Mail, Settings …)
    "wslhost", "conhost",     # Windows console wrappers
    "gnome-shell", "plasmashell",  # Linux desktop shells
    # Framework / browser-as-platform wrappers
    "cefsharp", "nwjs", "tauri",
    "msedge",  # Edge browser AND Edge PWAs — title disambiguates
})

# Known app names → activity_category, for NON-BROWSER windows.
# Browsers get their category from the site (see SITE_CATEGORIES).
KNOWN_APP_CATEGORIES = {
    # Terminal emulators
    "alacritty": "terminal", "kitty": "terminal", "wezterm": "terminal",
    "hyper": "terminal", "iterm2": "terminal", "iterm": "terminal",
    "terminal": "terminal", "windowsterminal": "terminal",
    "cmd": "terminal", "powershell": "terminal", "pwsh": "terminal",
    "wt": "terminal", "ghostty": "terminal", "warp": "terminal",
    "gnome-terminal": "terminal", "konsole": "terminal", "tilix": "terminal",
    "foot": "terminal", "st": "terminal", "urxvt": "terminal", "xterm": "terminal",
    # Code editors and IDEs
    "visual studio code": "coding", "code": "coding", "cursor": "coding",
    "pycharm": "coding", "intellij idea": "coding", "webstorm": "coding",
    "xcode": "coding", "android studio": "coding", "sublime text": "coding",
    "zed": "coding", "neovim": "coding", "datagrip": "coding",
    # Communication
    "discord": "communication", "slack": "communication",
    "teams": "communication", "microsoft teams": "communication",
    "telegram": "communication", "whatsapp": "communication",
    "signal": "communication", "messages": "communication", "mail": "communication",
    "thunderbird": "communication", "outlook": "communication",
    # Writing / Notes
    "anytype": "writing", "obsidian": "writing", "notion": "writing",
    "typora": "writing", "marktext": "writing", "logseq": "writing",
    "notepad": "writing", "wordpad": "writing", "textedit": "writing",
    "microsoft word": "writing", "pages": "writing", "notes": "writing",
    "libreoffice": "writing",
    # Media
    "spotify": "media", "vlc": "media", "mpv": "media", "music": "media",
    "quicktime player": "media", "iina": "media",
    # Design
    "figma": "design", "gimp": "design", "inkscape": "design", "sketch": "design",
    "photoshop": "design", "illustrator": "design",
    # Meetings
    "zoom": "meeting", "facetime": "meeting",
}

# Site → category for browser windows. Keys match the URL host (or a parent
# domain); without a URL, the same names are looked for in the window title.
SITE_CATEGORIES = {
    # Media
    "youtube.com": "media", "netflix.com": "media", "twitch.tv": "media",
    "open.spotify.com": "media", "music.youtube.com": "media",
    # Communication
    "mail.google.com": "communication", "outlook.live.com": "communication",
    "outlook.office.com": "communication", "slack.com": "communication",
    "discord.com": "communication", "web.telegram.org": "communication",
    "web.whatsapp.com": "communication", "teams.microsoft.com": "communication",
    # Meetings
    "meet.google.com": "meeting", "zoom.us": "meeting",
    # Coding
    "github.com": "coding", "gitlab.com": "coding", "stackoverflow.com": "coding",
    "bitbucket.org": "coding",
    # Writing
    "docs.google.com": "writing", "notion.so": "writing", "notion.site": "writing",
    "atlassian.net": "writing",
    # Design
    "figma.com": "design", "canva.com": "design",
}

# Window title words → category, for browser windows without a URL.
_TITLE_SITE_WORDS = {
    "youtube": "media", "netflix": "media", "twitch": "media",
    "gmail": "communication", "slack": "communication", "discord": "communication",
    "whatsapp": "communication", "telegram": "communication",
    "google meet": "meeting", "github": "coding", "gitlab": "coding",
    "stack overflow": "coding", "google docs": "writing", "notion": "writing",
    "figma": "design",
}

# File extensions used to reject filename candidates from title extraction.
# Prevents reversed conventions like "mpv - movie.mkv" → extracting "movie.mkv".
_FILE_EXTENSIONS = frozenset({
    # Code
    'py', 'js', 'ts', 'rs', 'go', 'java', 'c', 'cpp', 'h', 'rb', 'php', 'cs',
    'kt', 'swift', 'lua', 'sh', 'bash', 'zsh', 'ps1',
    # Web / Config
    'html', 'css', 'json', 'yaml', 'yml', 'toml', 'xml', 'ini', 'cfg', 'conf',
    # Documents
    'txt', 'md', 'log', 'csv', 'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx',
    # Media
    'mp4', 'mkv', 'avi', 'mov', 'webm', 'mp3', 'flac', 'wav', 'ogg',
    # Images
    'png', 'jpg', 'jpeg', 'gif', 'svg', 'bmp', 'webp', 'ico',
})

# Window title suffixes that identify a browser window.
_BROWSER_TITLE_SUFFIXES = (
    "- Google Chrome", "- Mozilla Firefox", "- Firefox",
    "- Microsoft Edge", "- Brave", "- Opera", "- Safari",
    "- Vivaldi", "- Arc", "- Chromium", "- Zen Browser",
)

# Patterns in window titles that indicate content, not an app name.
# Used by _extract_simple_title() to reject non-app-name titles.
_SIMPLE_TITLE_REJECT = ('/', '\\', '@', ':', '$', '#', '~',
                        '.txt', '.py', '.js', '.ts', '.rs', '.go',
                        '.html', '.css', '.md', '.json', '.yaml')

# Known browser process names — secondary detection when title suffix is missing.
_BROWSER_PROCESS_NAMES = frozenset({
    "chrome", "chromium", "firefox", "firefox-esr",
    "brave", "opera", "safari", "vivaldi", "arc",
    "google chrome", "brave browser", "zen browser",
    "msedge", "microsoft edge",  # msedge is also in GENERIC_PROCESS_NAMES
})


def _os_app_name_is_trusted() -> bool:
    """Ask the platform adapter whether the OS app name beats title parsing."""
    try:
        return platform_adapter().trusts_os_app_name
    except Exception:
        return False


def _extract_app_from_title(window_title: Optional[str]) -> Optional[str]:
    """Extract app name from the 'content - AppName' title convention.

    Most desktop apps set their window title as ``<document> - <AppName>``.
    This function takes the *last* segment after `` - `` (or `` – ``/`` — ``).

    Returns None if:
      - title is None / empty
      - title has no dash separator
      - the extracted candidate looks like a filename
    """
    if not window_title:
        return None
    # Try each dash variant: hyphen, en-dash, em-dash
    for sep in (" - ", " – ", " — "):
        parts = window_title.rsplit(sep, 1)
        if len(parts) == 2:
            candidate = parts[1].strip()
            if 2 <= len(candidate) <= 40:
                # Reject filenames (e.g., "movie.mkv" from "mpv - movie.mkv")
                if '.' in candidate:
                    ext = candidate.rsplit('.', 1)[-1].lower()
                    if ext in _FILE_EXTENSIONS:
                        continue  # Try next separator
                return candidate
    return None


def _extract_simple_title(window_title: str) -> Optional[str]:
    """Use a short clean title as the app name when no other signal is available.

    Handles cases like title='AnyType' or title='Calculator' where there is
    no `` - `` separator.  Rejects titles that look like content (paths,
    terminal prompts, document names).
    """
    title = window_title.strip()
    if not title or len(title) > 25:
        return None
    title_lower = title.lower()
    if any(p in title_lower for p in _SIMPLE_TITLE_REJECT):
        return None
    if title_lower.startswith(('untitled', 'new ', 'document')):
        return None
    return title


def _is_browser_window(
    window_title: Optional[str],
    app_name_hint: Optional[str] = None,
) -> bool:
    """Detect browser windows via title suffix or OS process name.

    Primary detection: title suffix (e.g. ``"- Google Chrome"``).
    Fallback: OS process name in ``_BROWSER_PROCESS_NAMES``.

    The fallback handles cases where L1 title extraction shadows the
    browser process name — e.g. title='Build Log - Jenkins' in Firefox
    would miss the suffix check, but OS reports 'firefox'.
    """
    if window_title and any(window_title.endswith(s) for s in _BROWSER_TITLE_SUFFIXES):
        return True
    if app_name_hint and app_name_hint.lower().strip() in _BROWSER_PROCESS_NAMES:
        return True
    return False


def _lookup_known_category(app_name: str) -> Optional[str]:
    """Look up the known activity category for an app name.

    Handles:
      - Case differences  (``AnyType`` → ``anytype``)
      - Linux 15-char truncation  (``gnome-terminal-`` → ``gnome-terminal``)
      - Full names containing a dict key  (``Microsoft Teams`` contains ``teams``)
    """
    name = app_name.lower().strip().rstrip("-. ")

    # Exact match (fastest path)
    if name in KNOWN_APP_CATEGORIES:
        return KNOWN_APP_CATEGORIES[name]

    # Prefix match — handles truncation and version suffixes
    #   "gnome-terminal-" → startswith("gnome-terminal")  ✓
    #   "anytype 0.38"    → startswith("anytype")         ✓
    for known, cat in KNOWN_APP_CATEGORIES.items():
        if name.startswith(known + " ") or name.startswith(known + "."):
            return cat
        # Reverse prefix only for minor truncation (e.g. Linux 15-char comm limit).
        # Require name to be at least 5 chars AND within 3 chars of the known key
        # to prevent false matches like "not" → "notion" or "sl" → "slack".
        if known.startswith(name) and len(name) >= 5 and len(name) >= len(known) - 3:
            return cat

    # Word-in-phrase — handles full names like "Microsoft Teams"
    #   re.search(r'\bteams\b', "microsoft teams")  ✓
    for known, cat in KNOWN_APP_CATEGORIES.items():
        if re.search(r'\b' + re.escape(known) + r'\b', name):
            return cat

    return None


def _site_category(url: Optional[str], window_title: Optional[str]) -> Optional[str]:
    """Category of the site in a browser window: URL host first, then title words."""
    if url:
        try:
            host = (urlparse(url if "://" in url else f"https://{url}").hostname or "").lower()
        except ValueError:
            host = ""
        host = host.removeprefix("www.")
        while host:
            if host in SITE_CATEGORIES:
                return SITE_CATEGORIES[host]
            host = host.partition(".")[2] if host.count(".") > 1 else ""
    if window_title:
        low = window_title.lower()
        for word, cat in _TITLE_SITE_WORDS.items():
            if re.search(r'\b' + re.escape(word) + r'\b', low):
                return cat
    return None


def resolve_app_name(app_name_hint: Optional[str], window_title: Optional[str]) -> str:
    """The app name for a frame, from the OS app name and the window title."""
    hint = (app_name_hint or "").strip()
    generic = hint.lower() in GENERIC_PROCESS_NAMES

    # L0: platforms with a clean OS app name (macOS) use it before title parsing
    if hint and not generic and _os_app_name_is_trusted():
        return hint
    # L1: Title " - " extraction ("main.py - Visual Studio Code" → "Visual Studio Code")
    title_app = _extract_app_from_title(window_title)
    if title_app:
        return title_app
    # L2: OS process name (skip generic wrappers like 'electron', 'java')
    if hint and not generic:
        return hint
    # L3: Simple title ("AnyType", "Calculator" — for when OS is None or generic)
    if window_title:
        simple = _extract_simple_title(window_title)
        if simple:
            return simple
    # L4: whatever the OS said, even a generic wrapper
    return hint or "unknown"


def categorize(app_name: str, app_name_hint: Optional[str] = None,
               window_title: Optional[str] = None, url: Optional[str] = None) -> str:
    """Activity category for a frame. "other" when no rule knows the app."""
    # Only browsers report a URL, so a URL also marks a browser window.
    # A web app in its own window (an Edge PWA) has its name as the app name.
    if url or _is_browser_window(window_title, app_name_hint) or _is_browser_window(None, app_name):
        return _site_category(url, window_title) or _lookup_known_category(app_name) or "browsing"
    return _lookup_known_category(app_name) or "other"


def label_frame(app_name_hint: Optional[str] = None, window_title: Optional[str] = None,
                url: Optional[str] = None) -> ActivityRecord:
    """App name and category for a frame. Summary, details, mood, confidence
    and scene description stay empty: nothing looks at the image."""
    app = resolve_app_name(app_name_hint, window_title)
    return ActivityRecord(
        app_name=app,
        activity_category=categorize(app, app_name_hint, window_title, url),
    )
