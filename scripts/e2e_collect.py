#!/usr/bin/env python3
"""
End-to-end check of ScreenMind data collection on this machine.

It starts ScreenMind with a throwaway data dir and its own port, runs a short
scripted scenario on the real desktop, stops ScreenMind and then reads the DB.
For each data point from docs/architecture/capture.md it prints PASS, FAIL or
SKIP with evidence.

    PASS  the data point was collected
    FAIL  it was checked and is missing or wrong
    SKIP  it could not be checked in this run (no call, no input, backlog...)

Run it from the repo root:

    uv run python scripts/e2e_collect.py --write-status

--write-status writes docs/status/<os>.md and docs/status/summary.md.
See docs/status/README.md for the rules.

Safety:
- Never uses ~/.screenmind or port 7777. Data goes to a temp dir that is
  deleted at the end (--keep-data keeps it).
- Types only into a scratch file it opens itself (TextEdit / Notepad) and
  into a password box it opens itself. Before every injected key or click it
  checks that the scratch editor is in front, and stops input if it is not.
- Clicks only inside the scratch editor window.
- Windows: ScreenMind ignores injected input there, so the click and typing
  steps ask you to do them by hand.
- Evidence in the report shows only test data (the scratch file, example.com,
  the markers). Secrets typed by the test are masked in the report.
"""

import argparse
import json
import os
import platform
import random
import re
import shutil
import socket
import sqlite3
import string
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parents[1]
STATUS_DIR = REPO / "docs" / "status"
GAPS_LINK = "../architecture/capture.md#8-gaps"
TEST_URL = "https://example.com/"
TEST_HOST = "example.com"
TEST_PAGE_TITLE = "Example Domain"

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

if sys.platform == "darwin":
    OS_KEY = "macos"
elif sys.platform == "win32":
    OS_KEY = "windows"
else:
    OS_KEY = "linux"

# Data points in report order: id, label, known gaps per OS (ids from
# docs/architecture/capture.md section 8). Browser URL rows are added per
# browser at run time with the "browser_url" gaps.
CHECKS = [
    ("screenshots", "Screenshots, every display", {"macos": ["G1"], "windows": []}),
    ("grab_backend", "Screen grab backend", {"macos": ["G1"], "windows": []}),
    ("app_name", "App name", {"macos": [], "windows": []}),
    ("window_title", "Window title", {"macos": [], "windows": []}),
    ("a11y_text", "Screen text from accessibility", {"macos": ["G7", "G12"], "windows": ["G7", "G12"]}),
    ("ocr", "OCR text", {"macos": ["G14"], "windows": ["G14"]}),
    ("browser_url", "Browser URL", {"macos": ["G8", "G9"], "windows": ["G7"]}),
    ("ui_click", "UI event: click with element role/name", {"macos": ["G20"], "windows": ["G20", "G25"]}),
    ("ui_app_switch", "UI event: app switch", {"macos": ["G20"], "windows": ["G20"]}),
    # Typed text and clipboard are on by default; no PII detection yet (G31)
    ("ui_text", "UI event: typed text", {"macos": ["G20", "G31"], "windows": ["G20", "G31"]}),
    ("ui_clipboard", "UI event: clipboard", {"macos": ["G20", "G31"], "windows": ["G20", "G31"]}),
    ("ui_linked", "UI events linked to frames", {"macos": ["G21", "G36"], "windows": ["G21", "G36"]}),
    ("redact_typed", "Typed secret is redacted", {"macos": [], "windows": []}),
    ("password_field", "Password field is not stored", {"macos": [], "windows": []}),
    ("calls", "Call detection", {"macos": [], "windows": ["G16", "G17"]}),
    ("analysis", "Analysis (Gemma)", {"macos": ["G15"], "windows": ["G15"]}),
    ("retention", "Retention cleanup at startup", {"macos": [], "windows": []}),
]
CHECK_LABELS = {cid: label for cid, label, _ in CHECKS}
CHECK_GAPS = {cid: gaps for cid, _, gaps in CHECKS}
CHECK_ORDER = [cid for cid, _, _ in CHECKS]


def log(msg=""):
    print(msg, flush=True)  # noqa: T201 (CLI output)


def step(msg):
    log(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


# ── Result collection ────────────────────────────────────────────────────────

class Report:
    def __init__(self, secrets):
        self.rows = []
        self._secrets = [s for s in secrets if s]

    def mask(self, text):
        text = str(text)
        for s in self._secrets:
            text = text.replace(s, "<secret>")
        return text.replace("|", "/").replace("\n", " ")

    def add(self, cid, result, evidence, label=None, gap_key=None):
        gaps = CHECK_GAPS.get(gap_key or cid, {}).get(OS_KEY, [])
        self.rows.append({
            "id": cid,
            "label": label or CHECK_LABELS.get(cid, cid),
            "result": result,
            "evidence": self.mask(evidence),
            "gaps": gaps,
        })

    def failed(self):
        return [r for r in self.rows if r["result"] == FAIL]


# ── ScreenMind instance ──────────────────────────────────────────────────────

def _port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def pick_port():
    """A free port in 7900-7999. Dev instances use 7800-7899, main uses 7777."""
    start = random.randrange(100)
    for i in range(100):
        port = 7900 + (start + i) % 100
        if _port_free(port):
            return port
    raise SystemExit("No free port in 7900-7999")


def _main_checkout():
    """Root of the main checkout (the first `git worktree list` entry)."""
    try:
        out = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=REPO,
                             capture_output=True, text=True, timeout=10).stdout
        first = out.splitlines()[0]
        if first.startswith("worktree "):
            return Path(first[len("worktree "):])
    except Exception:
        pass
    return REPO


def _dotenv():
    """KEY=VALUE pairs from .env (the repo's, or the main checkout's for a worktree)."""
    for root in (REPO, _main_checkout()):
        path = root / ".env"
        if path.is_file():
            pairs = {}
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                pairs[k.strip()] = v.strip().strip('"').strip("'")
            return pairs
    return {}


# Settings every run forces, the same on each machine. The status header lists
# the instance's other non-default settings (from .env), see _settings_text().
TEST_ENV = {
    "CAPTURE_ON_START": "true",
    "CAPTURE_INTERVAL": "10",
    "UI_EVENTS_ENABLED": "true",
    "UI_EVENTS_TYPES": "click,app_switch,window_focus,text,clipboard",
    "EVENT_TRIGGERED_CAPTURE": "true",
    "SENSITIVE_FILTER_ENABLED": "true",
    "RETENTION_DAYS": "7",
    # Nothing extra runs
    "MEETING_TRANSCRIPTION": "false",
}


class Instance:
    def __init__(self, data_dir: Path, port: int):
        self.data_dir = data_dir
        self.port = port
        self.log_path = data_dir / "e2e-screenmind.log"
        self.proc = None
        self._log_file = None

    def env(self):
        env = os.environ.copy()
        # Personal settings (OCR languages...) like scripts/dev-instance.sh
        for k, v in _dotenv().items():
            env.setdefault(k, v)
        env.update({
            # Isolation
            "DATA_DIR": str(self.data_dir),
            "SCREENMIND_DATA_DIR": str(self.data_dir),
            "API_HOST": "127.0.0.1",
            "API_PORT": str(self.port),
            "SETUP_COMPLETE": "true",
            **TEST_ENV,
            "SCREENMIND_LOG_LEVEL": "INFO",
            "PYTHONUNBUFFERED": "1",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        })
        env.pop("SCREENMIND_LOG_FILE", None)
        return env

    def start(self):
        self._log_file = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "screenmind"],
            cwd=REPO, env=self.env(),
            stdin=subprocess.DEVNULL, stdout=self._log_file, stderr=subprocess.STDOUT,
        )

    def api(self, path, method="GET", timeout=5):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=b"" if method == "POST" else None)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "null")

    def wait_ready(self, timeout=240):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"ScreenMind exited with code {self.proc.returncode}. "
                                   f"See {self.log_path}")
            try:
                self.api("/api/status")
                return
            except (urllib.error.URLError, OSError, ValueError):
                time.sleep(1)
        raise RuntimeError(f"ScreenMind did not answer on port {self.port} in {timeout}s")

    def stop(self, timeout=90):
        if not self.proc or self.proc.poll() is not None:
            return
        try:
            self.api("/api/shutdown", method="POST")
        except Exception:
            pass
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log("ScreenMind did not stop in time; killing it.")
            self.proc.kill()
            self.proc.wait(timeout=10)
        if self._log_file:
            self._log_file.close()

    def log_text(self):
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


# ── OS drivers ───────────────────────────────────────────────────────────────

class Driver:
    editor_app = ""
    injects_input = False

    def front(self):
        """(app_name, title) of the front window, lowercased app."""
        from screenmind.platform_support import adapter
        try:
            w = adapter().get_front_window() or {}
        except Exception:
            w = {}
        return (w.get("app_name") or "").lower(), w.get("title") or ""

    def editor_in_front(self):
        return self.front()[0] == self.editor_app.lower()

    def wait_front(self, app, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.front()[0] == app.lower():
                return True
            time.sleep(0.3)
        return False

    def browser_names(self):
        """Lowercased app names the adapter treats as browsers."""
        if OS_KEY == "macos":
            from screenmind.platform_support.macos import BROWSER_APPS
            return set(BROWSER_APPS)
        from screenmind.platform_support.windows import _BROWSER_EXES
        return set(_BROWSER_EXES)

    def display_count(self):
        try:
            import mss
            with mss.mss() as sct:
                return max(1, len(sct.monitors) - 1)
        except Exception:
            return 1


class MacDriver(Driver):
    editor_app = "TextEdit"

    def __init__(self):
        import ApplicationServices as AS
        import Quartz as Q
        self.AS, self.Q = AS, Q
        self.injects_input = bool(AS.AXIsProcessTrusted())
        self._editor_was_running = self._running("TextEdit")

    def _running(self, app):
        return subprocess.run(["pgrep", "-x", app], capture_output=True).returncode == 0

    def permissions(self):
        Q, AS = self.Q, self.AS
        return {
            "screen_recording": bool(Q.CGPreflightScreenCaptureAccess()),
            "accessibility": bool(AS.AXIsProcessTrusted()),
            "input_monitoring": bool(Q.CGPreflightListenEventAccess()),
        }

    def host_app(self):
        if os.environ.get("CLAUDECODE"):
            return "Claude Code session"
        return os.environ.get("TERM_PROGRAM") or os.environ.get("__CFBundleIdentifier") or "unknown"

    def open_url(self, url, browser=None):
        cmd = ["open", url] if not browser else ["open", "-a", browser, url]
        subprocess.run(cmd, check=False)

    def open_editor(self, path):
        subprocess.run(["open", "-a", "TextEdit", str(path)], check=False)

    def editor_bounds(self, name):
        """Bounds of the scratch file's TextEdit window (the front one if
        titles are hidden)."""
        Q = self.Q
        wins = Q.CGWindowListCopyWindowInfo(
            Q.kCGWindowListOptionOnScreenOnly | Q.kCGWindowListExcludeDesktopElements,
            Q.kCGNullWindowID) or []
        found = []
        for w in wins:
            if w.get("kCGWindowOwnerName") == "TextEdit" and w.get("kCGWindowLayer", 0) == 0:
                b = w.get("kCGWindowBounds") or {}
                if b.get("Width", 0) > 200 and b.get("Height", 0) > 150:
                    found.append((name in (w.get("kCGWindowName") or ""),
                                  (b["X"], b["Y"], b["Width"], b["Height"])))
        named = [b for match, b in found if match]
        return named[0] if named else (found[0][1] if found else None)

    def _post(self, ev):
        self.Q.CGEventPost(self.Q.kCGHIDEventTap, ev)

    def click(self, x, y):
        Q = self.Q
        for kind in (Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp):
            self._post(Q.CGEventCreateMouseEvent(None, kind, (x, y), Q.kCGMouseButtonLeft))
            time.sleep(0.05)

    def type_text(self, text, guard):
        """Type text, checking guard() before every key. False if guard failed."""
        Q = self.Q
        for ch in text:
            if not guard():
                return False
            for down in (True, False):
                ev = Q.CGEventCreateKeyboardEvent(None, 0, down)
                Q.CGEventKeyboardSetUnicodeString(ev, len(ch), ch)
                self._post(ev)
            time.sleep(0.03)
        return True

    def press_enter(self, guard):
        Q = self.Q
        if not guard():
            return False
        for down in (True, False):
            self._post(Q.CGEventCreateKeyboardEvent(None, 36, down))
        time.sleep(0.1)
        return True

    def open_password_box(self):
        """A dialog with a secure text field, owned by our own osascript process."""
        script = (
            'activate\n'
            'display dialog "ScreenMind e2e check: password field test. '
            'Type the test secret and press Return." default answer "" '
            'with hidden answer giving up after 60'
        )
        return subprocess.Popen(["osascript", "-e", script],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def secure_field_focused(self, pid):
        """True if the focused element is a password field in process pid."""
        AS = self.AS
        err, el = AS.AXUIElementCopyAttributeValue(
            AS.AXUIElementCreateSystemWide(), "AXFocusedUIElement", None)
        if err or el is None:
            return False
        err, sub = AS.AXUIElementCopyAttributeValue(el, "AXSubrole", None)
        err2, el_pid = AS.AXUIElementGetPid(el, None)
        return sub == "AXSecureTextField" and not err2 and el_pid == pid

    def set_clipboard(self, text):
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=False)

    def get_clipboard(self):
        return subprocess.run(["pbpaste"], capture_output=True).stdout.decode("utf-8", "replace")

    def close_editor(self, path):
        name = Path(path).name
        script = f'tell application "TextEdit" to close (every document whose name is "{name}") saving no'
        if not self._editor_was_running:
            script = 'tell application "TextEdit" to quit saving no'
        try:
            ok = subprocess.run(["osascript", "-e", script], capture_output=True,
                                timeout=30).returncode == 0
        except subprocess.TimeoutExpired:
            ok = False
        if not ok:
            log(f"Could not close TextEdit. Close '{name}' by hand and don't save it.")


class WinDriver(Driver):
    editor_app = "notepad"
    # The Windows hooks skip injected input (LLKHF_INJECTED), so a person has
    # to click and type.
    injects_input = False

    def permissions(self):
        try:
            import ctypes
            admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            admin = None
        return {"no_prompts_needed": True, "running_as_admin": admin}

    def host_app(self):
        if os.environ.get("CLAUDECODE"):
            return "Claude Code session"
        if os.environ.get("WT_SESSION"):
            return "Windows Terminal"
        return os.environ.get("TERM_PROGRAM") or "console"

    def open_url(self, url, browser=None):
        if browser:
            subprocess.Popen(["cmd", "/c", "start", "", browser, url])
        else:
            os.startfile(url)  # default browser

    def open_editor(self, path):
        subprocess.Popen(["notepad.exe", str(path)])

    def open_password_box(self):
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$f = New-Object Windows.Forms.Form;"
            "$f.Text = 'ScreenMind e2e password test';"
            "$f.Width = 460; $f.Height = 130; $f.TopMost = $true;"
            "$t = New-Object Windows.Forms.TextBox;"
            "$t.UseSystemPasswordChar = $true; $t.Left = 20; $t.Top = 20; $t.Width = 400;"
            "$t.Add_KeyDown({ if ($_.KeyCode -eq 'Enter') { $f.Close() } });"
            "$f.Controls.Add($t);"
            "$f.Add_Shown({ $f.Activate(); $t.Focus() });"
            "$timer = New-Object Windows.Forms.Timer; $timer.Interval = 90000;"
            "$timer.Add_Tick({ $f.Close() }); $timer.Start();"
            "[void]$f.ShowDialog()"
        )
        return subprocess.Popen(["powershell", "-NoProfile", "-STA", "-Command", ps])

    def set_clipboard(self, text):
        subprocess.run(["powershell", "-NoProfile", "-Command", "$input | Set-Clipboard"],
                       input=text, text=True, encoding="utf-8", check=False)

    def get_clipboard(self):
        return subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                              capture_output=True, text=True, encoding="utf-8").stdout.rstrip("\r\n")

    def close_editor(self, path):
        log(f"You can close the Notepad tab '{Path(path).name}' now. Don't save it.")


# ── Scenario ─────────────────────────────────────────────────────────────────

def _run_id():
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(6))


class Scenario:
    def __init__(self, driver: Driver, args, work_dir: Path):
        self.d = driver
        self.args = args
        rid = _run_id()
        self.run_id = rid
        self.a11y_marker = f"e2e-a11y-{rid}"
        self.typed_marker = f"e2e typed {rid}"
        self.typed_secret = f"Zq{rid}x9!"
        self.field_secret = f"Pw{rid}k7#"
        self.clip_marker = f"e2e clipboard {rid}"
        self.file = work_dir / f"screenmind-e2e-{rid}.txt"
        self.notes = []          # what happened, for the report
        self.input_mode = "none"  # injected / manual / none
        self.input_done = False
        self.password_done = False
        self.default_browser = None
        self.browsers_opened = []
        self.saved_clipboard = None

    def write_scratch_file(self):
        para = (
            f"ScreenMind end-to-end check, run {self.run_id}. "
            f"Marker for the accessibility check: {self.a11y_marker}. "
            "This scratch file was written by scripts/e2e_collect.py and can be deleted. "
            "The check opens it, clicks inside it and types a few test lines, so that "
            "ScreenMind has known text, a known window title and known UI events to find "
            "in its database afterwards. Nothing here is real data. "
            "The quick brown fox jumps over the lazy dog, again and again, so there is "
            "enough text on screen for the accessibility reader to count it as content."
        )
        self.file.write_text(para + "\n\n", encoding="utf-8")

    def _open_browser(self, browser):
        """Open the test URL; return the browser that came to the front, or None.
        Only known browser apps count, so a person switching apps is not taken
        for the browser."""
        known = self.d.browser_names()
        before = self.d.front()[0]
        self.d.open_url(TEST_URL, browser=browser)
        deadline = time.time() + 15
        while time.time() < deadline:
            time.sleep(0.5)
            app = self.d.front()[0]
            if app in known and (app != before or time.time() > deadline - 12):
                time.sleep(2)  # let the page load
                return app
        return None

    def _manual_ok(self):
        if self.args.no_input:
            return False
        if not sys.stdin or not sys.stdin.isatty():
            self.notes.append("No terminal input, so the by-hand steps were skipped.")
            return False
        return True

    def _wait_enter(self, prompt):
        try:
            input(prompt)
            return True
        except EOFError:
            return False

    def run(self):
        self.write_scratch_file()
        log("")
        log("Hands off the mouse and keyboard until it says 'You can use the computer again'.")
        log("The check switches apps itself; a click of yours changes what gets captured.")
        for n in (5, 4, 3, 2, 1):
            print(f"  starting in {n}...", end="\r", flush=True)  # noqa: T201
            time.sleep(1)
        log("")

        step(f"Opening {TEST_URL} in the default browser")
        self.default_browser = self._open_browser(None)
        if not self.default_browser:
            self.notes.append("The default browser did not come to the front.")
        time.sleep(self.args.dwell)

        for b in self.args.browsers:
            step(f"Opening {TEST_URL} in {b}")
            name = self._open_browser(b)
            if name:
                self.browsers_opened.append(name)
            else:
                self.notes.append(f"{b} did not come to the front.")
            time.sleep(self.args.dwell)

        use_manual = self.args.manual or not self.d.injects_input
        if use_manual and OS_KEY == "macos" and not self.args.manual and not self.args.no_input:
            self.notes.append("This process has no Accessibility permission, so it cannot send "
                              "clicks or keys. The input steps ran by hand or were skipped.")
        manual = use_manual and self._manual_ok()
        if manual:
            log("")
            log("=" * 70)
            log(f"By hand, part 1. When {self.d.editor_app} opens with the scratch file:")
            log("  1. Click once inside its text.")
            log(f"  2. Type:  {self.typed_marker}   and press Enter.")
            log(f"  3. Type:  password: {self.typed_secret}   and press Enter.")
            log(f"You have {self.args.manual_seconds} seconds. Don't click in other apps.")
            log("=" * 70)
            manual = self._wait_enter("Press Enter here to start... ")

        step(f"Opening the scratch file in {self.d.editor_app}")
        self.d.open_editor(self.file)
        if not self.d.wait_front(self.d.editor_app, timeout=15):
            self.notes.append(f"{self.d.editor_app} did not come to the front.")
        time.sleep(self.args.dwell)

        if self.args.no_input:
            self.notes.append("Input steps were skipped (--no-input).")
        elif not use_manual:
            self.input_mode = "injected"
            self.input_done = self._inject_typing()
        elif manual:
            self.input_mode = "manual"
            step(f"Waiting {self.args.manual_seconds}s for your click and typing")
            time.sleep(self.args.manual_seconds)
            self.input_done = True

        # Password box: a secure field we open ourselves
        if self.args.no_input:
            pass
        elif not use_manual:
            self.password_done = self._inject_password()
        elif manual:
            log("")
            log("=" * 70)
            log("By hand, part 2. A small password box opens.")
            log(f"  Type:  {self.field_secret}   and press Enter. The box closes.")
            log("=" * 70)
            if self._wait_enter("Press Enter here to open it... "):
                proc = self.d.open_password_box()
                try:
                    proc.wait(timeout=90)
                except subprocess.TimeoutExpired:
                    proc.kill()
                self.password_done = True

        step("Back to the scratch editor, then setting the clipboard")
        if OS_KEY == "macos":
            self.d.open_editor(self.file)
        time.sleep(2)
        try:
            self.saved_clipboard = self.d.get_clipboard()
        except Exception:
            self.saved_clipboard = None
        self.d.set_clipboard(self.clip_marker)
        time.sleep(3)

        step(f"Waiting {self.args.settle}s for capture to settle")
        time.sleep(self.args.settle)

    def _inject_typing(self):
        d = self.d
        if not d.editor_in_front():
            self.notes.append("Scratch editor was not in front; no click or typing was sent.")
            return False
        bounds = d.editor_bounds(self.file.name)
        if not bounds:
            self.notes.append("Could not find the scratch editor window; no click was sent.")
            return False
        x, y, w, h = bounds
        step("Clicking inside the scratch editor")
        d.click(x + w / 2, y + h * 0.6)
        time.sleep(1.5)
        for line in (self.typed_marker, f"password: {self.typed_secret}"):
            step("Typing a test line into the scratch editor")
            if not (d.type_text(line, d.editor_in_front) and d.press_enter(d.editor_in_front)):
                self.notes.append("Scratch editor lost focus; typing stopped.")
                return False
            time.sleep(1.5)
        return True

    def _inject_password(self):
        d = self.d
        step("Opening a password box and typing a test secret into it")
        proc = d.open_password_box()
        time.sleep(2.5)
        try:
            def guard():
                return d.secure_field_focused(proc.pid)
            if not guard():
                self.notes.append("The password box did not get focus; nothing was typed into it.")
                return False
            if not (d.type_text(self.field_secret, guard) and d.press_enter(guard)):
                self.notes.append("The password box lost focus; typing stopped.")
                return False
            time.sleep(1.5)
            return True
        finally:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


# ── Retention seed ───────────────────────────────────────────────────────────

SEED_APP = "e2e-retention-seed"


def seed_old_rows(data_dir: Path):
    """Rows 30 days old, which startup retention (7 days) must delete."""
    from screenmind.storage.database import Database
    db_path = data_dir / "screenmind.db"
    db = Database(db_path=db_path)  # creates the schema
    db.close()
    old = datetime.now() - timedelta(days=30)
    ts = old.strftime("%Y-%m-%d %H:%M:%S")
    shot_dir = data_dir / "screenshots" / old.strftime("%Y-%m-%d")
    shot_dir.mkdir(parents=True, exist_ok=True)
    shot = shot_dir / "00-00-00_000.jpg"
    shot.write_bytes(b"\xff\xd8\xff\xd9")
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO activities (timestamp, screenshot_path, window_title, detected_app, status) "
                 "VALUES (?, ?, 'seed', ?, 'ok')", (ts, str(shot), SEED_APP))
    conn.execute("INSERT INTO ui_events (timestamp, type, app_name) VALUES (?, 'click', ?)",
                 (old.isoformat(), SEED_APP))
    conn.execute("INSERT INTO meetings (start_time, end_time, app_name) VALUES (?, ?, ?)",
                 (ts, ts, SEED_APP))
    conn.commit()
    conn.close()
    return shot


# ── Checks ───────────────────────────────────────────────────────────────────

TEXT_COLUMNS = {
    "activities": ["window_title", "ocr_text", "organized_text", "ocr_boxes", "user_actions",
                   "summary", "details", "visible_text", "scene_description", "active_url"],
    "ui_events": ["window_title", "element_name", "element_value", "text", "url"],
    "meetings": ["window_title", "transcript", "summary", "url"],
}
GEMMA_COLUMNS = {"summary", "details", "visible_text", "scene_description"}


def _ui_status_text(st):
    keys = ("backend", "running", "hook_running", "keys_tapped", "input_events",
            "events_recorded", "skipped", "permissions", "last_error")
    return ", ".join(f"{k}={st.get(k)}" for k in keys if k in st)


def _rows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _find_text(conn, needle):
    """[(table.column, count)] of every text column that contains needle."""
    hits = []
    for table, cols in TEXT_COLUMNS.items():
        for col in cols:
            try:
                n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE instr({col}, ?) > 0",
                                 (needle,)).fetchone()[0]
            except sqlite3.OperationalError:
                continue
            if n:
                hits.append((f"{table}.{col}", n))
    return hits


def _host(url):
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _short(text, n=80):
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 3] + "..."


def wait_for_rows(db_path, count_sql, args, timeout):
    """Wait until count_sql returns more than 0. True if it did."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
            n = conn.execute(count_sql, args).fetchone()[0]
            conn.close()
            if n:
                return True
        except sqlite3.Error:
            pass
        time.sleep(2)
    return False


def wait_for_editor_frame(db_path, editor_app, timeout):
    """Wait until a frame of the scratch editor is saved. True if one is."""
    return wait_for_rows(db_path, "SELECT COUNT(*) FROM activities WHERE lower(detected_app) = ?",
                         (editor_app.lower(),), timeout)


def wait_for_analysis(db_path, timeout):
    """Wait until no frame is pending, or timeout. Returns seconds waited."""
    start = time.time()
    last_print = 0
    while True:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
            pending, total = conn.execute(
                "SELECT SUM(status = 'pending'), COUNT(*) FROM activities WHERE detected_app IS NOT ?",
                (SEED_APP,)).fetchone()
            conn.close()
        except sqlite3.Error:
            pending, total = None, 0
        waited = time.time() - start
        if total and not pending:
            return waited
        if waited >= timeout:
            return waited
        if waited - last_print >= 15:
            step(f"Analysis: {pending or 0} of {total} frames still pending")
            last_print = waited
        time.sleep(3)


def run_checks(rep: Report, sc: Scenario, inst: Instance, ui_status: dict, displays: int, seed_shot: Path):
    db_path = inst.data_dir / "screenmind.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    log_text = inst.log_text()
    editor = sc.d.editor_app.lower()
    stem = sc.file.stem

    acts = _rows(conn, "SELECT * FROM activities WHERE detected_app IS NOT ? ORDER BY id", (SEED_APP,))
    events = _rows(conn, "SELECT * FROM ui_events WHERE app_name IS NOT ? ORDER BY id", (SEED_APP,))
    meetings = _rows(conn, "SELECT * FROM meetings WHERE app_name IS NOT ?", (SEED_APP,))
    editor_rows = [a for a in acts if (a["detected_app"] or "").lower() == editor]
    ui_running = bool(ui_status.get("running"))
    ui_why = _ui_status_text(ui_status)

    # Screenshots per display
    shots = [p for p in (inst.data_dir / "screenshots").rglob("*.jpg") if p != seed_shot]
    per = {}
    for p in shots:
        m = re.search(r"_m(\d+)\.jpg$", p.name)
        key = int(m.group(1)) if m else 1
        per[key] = per.get(key, 0) + 1
    got = ", ".join(f"display {k}: {v}" for k, v in sorted(per.items())) or "none"
    ok = len(per) >= displays and all(per.get(i, 0) for i in range(1, displays + 1))
    rep.add("screenshots", PASS if ok else FAIL,
            f"{displays} display(s); frames saved: {got}; {len(acts)} activity rows")

    # Grab backend
    backends = re.findall(r"Screen grab backend: (\w+)", log_text)
    switches = re.findall(r"(ScreenCaptureKit [^\n]*|Screen grab took [^\n]*)", log_text)
    want = {"macos": "sck", "windows": "mss"}.get(OS_KEY, "mss")
    if not backends:
        rep.add("grab_backend", FAIL, "no grab happened (no 'Screen grab backend' line in the log)")
    else:
        ev = f"used: {' -> '.join(backends)}"
        if switches:
            ev += f"; log: {_short(switches[0], 120)}"
        rep.add("grab_backend", PASS if backends == [want] else FAIL, ev + f"; expected {want}")

    # App name and window title
    apps_seen = sorted({a["detected_app"] for a in acts if a["detected_app"]})
    if editor_rows:
        rep.add("app_name", PASS, f"detected_app={editor_rows[0]['detected_app']} on {len(editor_rows)} rows")
    else:
        rep.add("app_name", FAIL, f"no row with the scratch editor; apps seen: {', '.join(apps_seen[:10]) or 'none'}")
    titled = [a for a in editor_rows if stem in (a["window_title"] or "")]
    if titled:
        rep.add("window_title", PASS, f"window_title='{_short(titled[0]['window_title'])}' on {len(titled)} rows")
    elif editor_rows:
        rep.add("window_title", FAIL, f"editor rows have titles {sorted({a['window_title'] for a in editor_rows})[:3]}")
    else:
        rep.add("window_title", FAIL, "no row with the scratch editor")

    # a11y text. analysis_method does not say where the text came from
    # (it is "full:fast" etc.), but only OCR writes ocr_boxes. So: marker in
    # ocr_text and no boxes = read through accessibility.
    marked = [a for a in editor_rows if sc.a11y_marker in (a["ocr_text"] or "")]
    via_a11y = [a for a in marked if not a["ocr_boxes"]]
    if via_a11y:
        a = via_a11y[0]
        rep.add("a11y_text", PASS,
                f"marker found without OCR on {len(via_a11y)} editor rows; text length {len(a['ocr_text'])} chars")
    elif marked:
        rep.add("a11y_text", FAIL, f"marker found only through OCR ({len(marked)} rows); a11y text was not used")
    elif editor_rows and all(a["status"] == "pending" for a in editor_rows):
        rep.add("a11y_text", SKIP, "editor frames were still pending at the end")
    elif editor_rows:
        rep.add("a11y_text", FAIL, f"marker not in ocr_text of {len(editor_rows)} editor rows")
    else:
        rep.add("a11y_text", FAIL, "no row with the scratch editor")

    # OCR: only OCR writes ocr_boxes
    ocr_rows = [a for a in acts if a["ocr_boxes"] and len(a["ocr_text"] or "") >= 20]
    analyzed = [a for a in acts if a["status"] == "ok"]
    if ocr_rows:
        test_ocr = [a for a in ocr_rows if TEST_PAGE_TITLE in (a["ocr_text"] or "")]
        extra = f"; '{TEST_PAGE_TITLE}' read on {len(test_ocr)} rows" if test_ocr else ""
        rep.add("ocr", PASS, f"OCR boxes on {len(ocr_rows)} of {len(analyzed)} analyzed rows "
                             f"(longest text {max(len(a['ocr_text']) for a in ocr_rows)} chars){extra}")
    elif analyzed:
        rep.add("ocr", FAIL, f"{len(analyzed)} rows analyzed, none has OCR boxes")
    else:
        rep.add("ocr", SKIP, "no frame was analyzed, so OCR never ran")

    # Browser URLs, one row per browser
    url_rows = [a for a in acts if _host(a["active_url"]) == TEST_HOST]
    ev_urls = [e for e in events if _host(e.get("url")) == TEST_HOST]
    page_rows = [a for a in acts if TEST_PAGE_TITLE.lower() in (a["window_title"] or "").lower()]
    browsers = []
    for b in [sc.default_browser] + sc.browsers_opened:
        if b and b not in browsers and b != editor:
            browsers.append(b)
    for a in url_rows + page_rows:
        name = (a["detected_app"] or "").lower()
        if name and name not in browsers:
            browsers.append(name)
    if not browsers:
        rep.add("browser_url", FAIL, f"no browser window with {TEST_HOST} was captured",
                label="Browser URL (default browser)")
    for b in browsers:
        mine = [a for a in url_rows if (a["detected_app"] or "").lower() == b]
        page = [a for a in page_rows if (a["detected_app"] or "").lower() == b]
        evs = [e for e in ev_urls if (e.get("app_name") or "").lower() == b]
        label = f"Browser URL ({b})"
        if mine:
            rep.add(f"browser_url:{b}", PASS,
                    f"active_url={mine[0]['active_url']} on {len(mine)} rows; ui_events.url on {len(evs)} events",
                    label=label, gap_key="browser_url")
        elif page:
            rep.add(f"browser_url:{b}", FAIL,
                    f"{len(page)} rows show '{TEST_PAGE_TITLE}' but active_url is empty; ui_events.url on {len(evs)} events",
                    label=label, gap_key="browser_url")
        else:
            rep.add(f"browser_url:{b}", FAIL, f"no frame of {b} on {TEST_HOST} was saved",
                    label=label, gap_key="browser_url")

    # UI events
    by_type = {}
    for e in events:
        by_type[e["type"]] = by_type.get(e["type"], 0) + 1
    counts = ", ".join(f"{k}: {v}" for k, v in sorted(by_type.items())) or "no events"

    def ui_unavailable(cid):
        if not ui_running:
            rep.add(cid, FAIL, f"UI event recorder is not running ({ui_why})")
            return True
        return False

    input_skipped = not sc.input_done
    skip_why = f"input step did not run (mode: {sc.input_mode})"

    if not ui_unavailable("ui_click"):
        clicks = [e for e in events if e["type"] == "click" and (e["app_name"] or "").lower() == editor]
        if clicks:
            c = clicks[0]
            rep.add("ui_click", PASS if c["element_role"] else FAIL,
                    f"click in {c['app_name']}: role={c['element_role']}, name={_short(c['element_name'], 40)!r}; all events: {counts}")
        elif input_skipped:
            rep.add("ui_click", SKIP, skip_why)
        else:
            rep.add("ui_click", FAIL, f"no click in the scratch editor; all events: {counts}")

    if not ui_unavailable("ui_app_switch"):
        sw = [e for e in events if e["type"] == "app_switch"]
        to_editor = [e for e in sw if (e["app_name"] or "").lower() == editor]
        if to_editor:
            rep.add("ui_app_switch", PASS, f"{len(sw)} app switches, {len(to_editor)} to {sc.d.editor_app}")
        else:
            rep.add("ui_app_switch", FAIL, f"{len(sw)} app switches, none to {sc.d.editor_app}; all events: {counts}")

    if not ui_unavailable("ui_text"):
        typed = [e for e in events if e["type"] == "text" and sc.typed_marker in (e["text"] or "")]
        if typed:
            t = typed[0]
            rep.add("ui_text", PASS, f"text={_short(t['text'], 40)!r} in {t['app_name']}, role={t['element_role']}")
        elif input_skipped:
            rep.add("ui_text", SKIP, skip_why)
        else:
            texts = [e for e in events if e["type"] == "text"]
            rep.add("ui_text", FAIL, f"typed marker not found; {len(texts)} text events; all events: {counts}")

    if not ui_unavailable("ui_clipboard"):
        clips = [e for e in events if e["type"] == "clipboard" and sc.clip_marker in (e["text"] or "")]
        if clips:
            rep.add("ui_clipboard", PASS, f"clipboard text={_short(clips[0]['text'], 40)!r} in {clips[0]['app_name']}")
        else:
            rep.add("ui_clipboard", FAIL, f"clipboard marker not found; all events: {counts}")

    linked = [a for a in acts if a["user_actions"]]
    if not ui_running:
        rep.add("ui_linked", FAIL, "UI event recorder is not running")
    elif linked:
        test_linked = [a for a in linked if sc.typed_marker in a["user_actions"] or stem in a["user_actions"]]
        rep.add("ui_linked", PASS,
                f"user_actions set on {len(linked)} of {len(acts)} rows; {len(test_linked)} mention the test")
    elif events:
        rep.add("ui_linked", FAIL, f"{len(events)} events recorded but no row has user_actions")
    else:
        rep.add("ui_linked", FAIL, "no UI events recorded")

    # Privacy: typed secret after "password:" must be redacted
    hits = _find_text(conn, sc.typed_secret)
    if hits:
        where = ", ".join(f"{c} ({n})" for c, n in hits)
        note = " (Gemma read it from the image; images are not redacted)" if all(
            c.split(".")[1] in GEMMA_COLUMNS for c, _ in hits) else ""
        rep.add("redact_typed", FAIL, f"secret stored in {where}{note}")
    else:
        red_ev = [e for e in events if "password: [REDACTED" in (e["text"] or "")]
        red_act = [a for a in editor_rows if "[REDACTED:password]" in (a["ocr_text"] or "")]
        if red_ev or red_act:
            rep.add("redact_typed", PASS,
                    f"secret not stored; redacted in {len(red_ev)} text events and {len(red_act)} editor rows")
        elif input_skipped:
            rep.add("redact_typed", SKIP, skip_why)
        else:
            rep.add("redact_typed", PASS, "secret not stored (no redaction mark seen either)")

    # Privacy: secure text field
    hits = _find_text(conn, sc.field_secret)
    placeholder = [e for e in events if (e["text"] or "") == "[password field]"]
    if hits:
        rep.add("password_field", FAIL, "secret stored in " + ", ".join(f"{c} ({n})" for c, n in hits))
    elif not sc.password_done:
        rep.add("password_field", SKIP, "password box step did not run")
    elif placeholder:
        rep.add("password_field", PASS,
                f"secret not stored; {len(placeholder)} '[password field]' events in {placeholder[0]['app_name']}")
    elif ui_running:
        why = (" (macOS secure input hides keys in password fields from event taps)"
               if OS_KEY == "macos" else "")
        rep.add("password_field", PASS, f"secret not stored; no event for the field either{why}")
    else:
        rep.add("password_field", SKIP, "UI events were not recorded, so this proves nothing")

    # Calls
    if meetings:
        m = meetings[0]
        rep.add("calls", PASS, f"{len(meetings)} call(s); first: app={m['app_name']}, "
                               f"duration={m['duration_minutes']} min, url={'yes' if m.get('url') else 'no'}")
    else:
        rep.add("calls", SKIP, "no call was active during the run")

    # Analysis
    status = {}
    for a in acts:
        status[a["status"]] = status.get(a["status"], 0) + 1
    stxt = ", ".join(f"{k}: {v}" for k, v in sorted(status.items())) or "no rows"
    ok_rows = [a for a in acts if a["status"] == "ok"]
    if ok_rows:
        sample = [a for a in ok_rows if a in editor_rows or a in url_rows or a in page_rows]
        ev = f"status {stxt}"
        # No summary text: Gemma may describe other windows on the screen
        if sample:
            s = sample[0]
            ev += (f"; sample ({s['detected_app']}): category={s['category']}, "
                   f"summary {len(s['summary'] or '')} chars")
        with_summary = sum(1 for a in ok_rows if a["summary"])
        ev += f"; summary set on {with_summary} of {len(ok_rows)} ok rows"
        rep.add("analysis", PASS, ev)
    else:
        why = "llama-server not reachable" if "Cannot reach llama-server" in log_text else "no frame finished"
        rep.add("analysis", FAIL, f"status {stxt}; {why}")

    # Retention
    left = (conn.execute("SELECT COUNT(*) FROM activities WHERE detected_app = ?", (SEED_APP,)).fetchone()[0]
            + conn.execute("SELECT COUNT(*) FROM ui_events WHERE app_name = ?", (SEED_APP,)).fetchone()[0]
            + conn.execute("SELECT COUNT(*) FROM meetings WHERE app_name = ?", (SEED_APP,)).fetchone()[0])
    if left == 0 and not seed_shot.exists():
        rep.add("retention", PASS, "30-day-old activity, ui_event, meeting and JPEG were deleted at startup")
    else:
        rep.add("retention", FAIL, f"{left} old seed rows left; old JPEG exists={seed_shot.exists()}")

    conn.close()


# ── Output ───────────────────────────────────────────────────────────────────

def _git(*args):
    try:
        return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except Exception:
        return ""


def os_version():
    if OS_KEY == "macos":
        return f"macOS {platform.mac_ver()[0]} ({platform.machine()})"
    if OS_KEY == "windows":
        rel, ver, _, _ = platform.win32_ver()
        return f"Windows {rel} build {ver} ({platform.machine()})"
    return f"{platform.system()} {platform.release()}"


def _settings_text(log_text):
    """Non-default settings of the run, from the instance's startup log line.

    Leaves out what every run forces (TEST_ENV, data dir, port), so what is
    left differs between machines: their .env. Empty if the line is missing.
    """
    m = re.search(r"Settings that differ from the defaults: (.*)", log_text)
    if not m:
        return ""
    forced = {k.lower() for k in TEST_ENV} | {"data_dir", "api_port"}
    items = [s for s in m.group(1).strip().split("; ")
             if s != "none" and s.split("=", 1)[0] not in forced]
    return "; ".join(items) or "all at code defaults"


def print_table(rows):
    w = max(len(r["label"]) for r in rows)
    log("")
    log(f"{'Data point'.ljust(w)}  Result  Evidence")
    log(f"{'-' * w}  ------  --------")
    for r in rows:
        log(f"{r['label'].ljust(w)}  {r['result']:<6}  {r['evidence']}")
    counts = {k: sum(r["result"] == k for r in rows) for k in (PASS, FAIL, SKIP)}
    log("")
    log(f"PASS {counts[PASS]}   FAIL {counts[FAIL]}   SKIP {counts[SKIP]}")


def _gap_cell(gaps):
    return ", ".join(f"[{g}]({GAPS_LINK})" for g in gaps) or ""


def write_status(path: Path, meta: dict, rows: list, notes: list):
    lines = [
        f"# ScreenMind collection status: {meta['os_label']}",
        "",
        "Generated by `scripts/e2e_collect.py --write-status`. Don't edit by hand; run the check again.",
        "How to read it: [README.md](README.md).",
        "",
        "| | |",
        "|---|---|",
    ]
    for key, label in (("state", "State"), ("date", "Date"), ("machine", "Machine"), ("os", "OS"),
                       ("git", "Git"), ("python", "Python"), ("host_app", "Started from"),
                       ("settings", "Settings not at default"),
                       ("permissions", "Permissions"), ("displays", "Displays"),
                       ("input_mode", "Input"), ("ui_events", "UI event recorder"),
                       ("duration", "Run time"), ("command", "Command")):
        if meta.get(key) not in (None, ""):
            val = meta[key]
            val = f"`{val}`" if key == "command" else val
            lines.append(f"| {label} | {val} |")
    lines += ["", "## Results", ""]
    if rows:
        lines += ["| Data point | Result | Evidence | Known gaps |", "|---|---|---|---|"]
        for r in rows:
            lines.append(f"| {r['label']} | **{r['result']}** | {r['evidence']} | {_gap_cell(r['gaps'])} |")
    else:
        lines.append("No results yet.")
    if notes:
        lines += ["", "## Notes", ""] + [f"- {n}" for n in notes]
    payload = {"meta": meta, "results": [{k: r[k] for k in ("id", "label", "result")} for r in rows]}
    lines += ["", f"<!-- e2e-results {json.dumps(payload, ensure_ascii=False)} -->", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _read_status(path: Path):
    m = re.search(r"<!-- e2e-results (.*?) -->", path.read_text(encoding="utf-8"), re.S)
    return json.loads(m.group(1)) if m else None


def write_summary():
    files = sorted(p for p in STATUS_DIR.glob("*.md") if p.name not in ("README.md", "summary.md"))
    machines = []
    for p in files:
        data = _read_status(p)
        if data:
            machines.append((p, data))
    labels, order = {}, []
    for _, data in machines:
        for r in data["results"]:
            if r["id"] not in labels:
                labels[r["id"]] = r["label"]
                order.append(r["id"])

    def rank(cid):
        base = cid.split(":")[0]
        return (CHECK_ORDER.index(base) if base in CHECK_ORDER else 99, cid)

    order.sort(key=rank)
    if not order:
        order = list(CHECK_ORDER)
        labels = dict(CHECK_LABELS)

    lines = [
        "# ScreenMind collection status: all machines",
        "",
        "Generated from the other files in this folder by `scripts/e2e_collect.py --summary`",
        "(also run by `--write-status`). Don't edit by hand. On a merge conflict, run",
        "`python scripts/e2e_collect.py --summary` and commit the result.",
        "",
    ]
    head = "| Data point | " + " | ".join(f"[{p.stem}]({p.name})" for p, _ in machines) + " |"
    lines += [head, "|---|" + "---|" * len(machines)]
    for key, label in (("state", "State"), ("date", "Date"), ("git", "Git")):
        lines.append(f"| *{label}* | " + " | ".join(str(d["meta"].get(key, "")) for _, d in machines) + " |")
    for cid in order:
        cells = []
        for _, data in machines:
            res = {r["id"]: r["result"] for r in data["results"]}
            cells.append(res.get(cid, "-"))
        lines.append(f"| {labels[cid]} | " + " | ".join(cells) + " |")
    lines += ["", "`-` means this machine has no result for that row (not run, or no such browser there).", ""]
    (STATUS_DIR / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    return STATUS_DIR / "summary.md"


def write_placeholder(os_key: str):
    """Status file for a machine that has not run the check yet."""
    cmd = {"windows": r"uv run python scripts\e2e_collect.py --write-status",
           "macos": "uv run python scripts/e2e_collect.py --write-status"}.get(os_key)
    meta = {"os_label": {"windows": "Windows", "macos": "macOS"}.get(os_key, os_key),
            "state": "not run yet", "date": "", "command": cmd}
    notes = [
        "Nobody has run the check on this machine yet.",
        "Once per machine: install uv (https://docs.astral.sh/uv/). It installs Python 3.14 itself "
        "(`uv python install 3.14` if you want it up front).",
        "Then `git pull origin custom` and `uv sync` in the repo root. Run the command above from "
        "there, in a normal terminal. "
        "It takes 3 to 5 minutes. "
        "Hands off the mouse and keyboard until it says you can use the computer again.",
        "Gemma analysis needs llama-server. If it is not running, ScreenMind starts its own and "
        "stops it at the end. Without llama-server the Analysis row is a FAIL; the rest still works.",
        "Then commit this file and `summary.md` (`git add docs/status`), and push to `custom`.",
    ]
    if os_key == "windows":
        notes.append("Windows ignores injected input, so the script stops twice and asks you to click "
                     "and type: once in Notepad, once in a small password box. It prints what to type. "
                     "Read it, press Enter, then do it.")
        notes.append("Optional: `--browsers chrome,msedge,firefox` also tests those browsers.")
    else:
        notes.append("Run it from Terminal.app, not from a Claude session (gap G1).")
    write_status(STATUS_DIR / f"{os_key}.md", meta, [], notes)


# ── Main ─────────────────────────────────────────────────────────────────────

def parse_args():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write-status", action="store_true",
                    help="write docs/status/<name>.md and docs/status/summary.md")
    ap.add_argument("--status-name", default=OS_KEY,
                    help=f"status file name without .md (default: {OS_KEY}). Use one name per machine.")
    ap.add_argument("--browsers", default="",
                    help="extra browsers to open the test URL in, comma-separated "
                         "(macOS app names like 'Safari,Google Chrome'; Windows: chrome,msedge,firefox)")
    ap.add_argument("--manual", action="store_true", help="do the click and typing by hand (macOS)")
    ap.add_argument("--no-input", action="store_true", help="skip all click, typing and password steps")
    ap.add_argument("--manual-seconds", type=int, default=30, help="time for the by-hand typing step")
    ap.add_argument("--dwell", type=float, default=12,
                    help="seconds to stay on each app (more than the 10s capture interval)")
    ap.add_argument("--settle", type=float, default=12, help="seconds to wait after the scenario")
    ap.add_argument("--analysis-timeout", type=float, default=240,
                    help="max seconds to wait for analysis of the captured frames")
    ap.add_argument("--keep-data", action="store_true", help="keep the temp data dir for inspection")
    ap.add_argument("--summary", action="store_true", help="only regenerate docs/status/summary.md")
    ap.add_argument("--placeholder", metavar="OS", help=argparse.SUPPRESS)
    args = ap.parse_args()
    args.browsers = [b.strip() for b in args.browsers.split(",") if b.strip()]
    return args


def main():
    args = parse_args()
    if args.summary:
        log(f"Wrote {write_summary()}")
        return 0
    if args.placeholder:
        write_placeholder(args.placeholder)
        log(f"Wrote {STATUS_DIR / (args.placeholder + '.md')} and {write_summary()}")
        return 0
    if OS_KEY not in ("macos", "windows"):
        log("This check supports macOS and Windows only.")
        return 2

    started = time.time()
    data_dir = Path(tempfile.mkdtemp(prefix="screenmind-e2e-"))
    try:
        return _run(args, started, data_dir)
    finally:
        if args.keep_data:
            log(f"Data kept in {data_dir}")
        else:
            shutil.rmtree(data_dir, ignore_errors=True)


def _run(args, started, data_dir):
    work_dir = Path(tempfile.mkdtemp(prefix="screenmind-e2e-work-"))
    home_data = Path.home() / ".screenmind"
    assert data_dir.resolve() != home_data.resolve()
    port = pick_port()

    # Point this process at the temp dir too, before anything imports screenmind
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["SCREENMIND_DATA_DIR"] = str(data_dir)
    os.environ.setdefault("SCREENMIND_LOG_LEVEL", "WARNING")
    sys.path.insert(0, str(REPO))

    driver = MacDriver() if OS_KEY == "macos" else WinDriver()
    sc = Scenario(driver, args, work_dir)
    rep = Report([sc.typed_secret, sc.field_secret])
    displays = driver.display_count()
    perms = driver.permissions()

    if OS_KEY == "macos" and os.environ.get("CLAUDECODE"):
        sc.notes.append("Started from a Claude Code session. On macOS such a process can't use "
                        "ScreenCaptureKit (gap G1), so the grab backend row is not valid. "
                        "Run it from Terminal.app for the real result.")
    log("ScreenMind e2e collection check")
    log(f"  data dir: {data_dir} (temporary)")
    log(f"  port:     {port}")
    log(f"  run id:   {sc.run_id}")
    if OS_KEY == "macos" and not perms.get("screen_recording"):
        log("  WARNING: no Screen Recording permission for this terminal. Screenshots will fail.")
    log("")

    inst = Instance(data_dir, port)
    ui_status = {}
    try:
        seed_shot = seed_old_rows(data_dir)
        step("Starting ScreenMind")
        inst.start()
        inst.wait_ready()
        step("ScreenMind is up; waiting for its first frame")
        # Startup grabs (and a possible SCK timeout) must finish first, or the
        # browser's app switch is merged into them and never gets its own frame
        if not wait_for_rows(data_dir / "screenmind.db", "SELECT COUNT(*) FROM activities "
                             "WHERE detected_app IS NOT ?", (SEED_APP,), 45):
            log("  No frame after 45s. Screen capture may be broken; going on anyway.")
        time.sleep(4)
        try:
            ui_status = inst.api("/api/ui-events/status")
        except Exception as e:
            ui_status = {"running": False, "last_error": f"status call failed: {e}"}
        if not ui_status.get("running"):
            log(f"  UI event recorder is not running: {ui_status}")

        sc.run()

        if not wait_for_editor_frame(data_dir / "screenmind.db", driver.editor_app, 45):
            # Bring it to the front once more; a frame with it must exist for most checks
            step(f"No {driver.editor_app} frame yet; showing it again for 30s")
            if OS_KEY == "macos":
                driver.open_editor(sc.file)
            if not wait_for_editor_frame(data_dir / "screenmind.db", driver.editor_app, 30):
                sc.notes.append(f"No {driver.editor_app} frame was saved in 75s after the scenario.")
        # Stop collecting before the person takes the computer back. Otherwise
        # their own windows keep adding frames, analysis never catches up, and
        # the test DB fills with real screen content.
        try:
            inst.api("/api/capture/pause", method="POST")
        except Exception as e:
            sc.notes.append(f"Could not pause capture after the scenario: {e}")
        log("You can use the computer again. The rest is waiting for analysis.")

        step("Waiting for analysis")
        waited = wait_for_analysis(data_dir / "screenmind.db", args.analysis_timeout)
        step(f"Analysis wait done after {waited:.0f}s")
        try:
            ui_status = {**ui_status, **inst.api("/api/ui-events/status")}
        except Exception:
            pass
    except KeyboardInterrupt:
        log("Interrupted.")
        inst.stop()
        return 130
    except Exception as e:
        log(f"ERROR: {e}")
        inst.stop()
        tail = inst.log_text()[-3000:]
        if tail:
            log("Last lines of the ScreenMind log:")
            log(tail)
        return 2
    finally:
        try:
            driver.close_editor(sc.file)
        except Exception:
            pass
        shutil.rmtree(work_dir, ignore_errors=True)

    step("Stopping ScreenMind")
    inst.stop()
    if sc.saved_clipboard:
        driver.set_clipboard(sc.saved_clipboard)  # text only; images are not saved
    run_checks(rep, sc, inst, ui_status, displays, seed_shot)
    print_table(rep.rows)
    for n in sc.notes:
        log(f"Note: {n}")

    if args.write_status:
        sha = _git("rev-parse", "--short", "HEAD")
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        dirty = " (uncommitted changes)" if _git("status", "--porcelain", "--untracked-files=no") else ""
        perm_txt = ", ".join(f"{k}={'yes' if v else 'no' if v is not None else '?'}"
                             for k, v in perms.items())
        if ui_status.get("permissions"):
            perm_txt += "; ScreenMind sees: " + ", ".join(
                f"{k}={'yes' if v else 'no'}" for k, v in ui_status["permissions"].items() if k != "all_granted")
        meta = {
            "os_label": {"macos": "macOS", "windows": "Windows"}[OS_KEY] +
                        ("" if args.status_name == OS_KEY else f" ({args.status_name})"),
            "state": "ran from a Claude session" if os.environ.get("CLAUDECODE") else "ran",
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "machine": platform.node(),
            "os": os_version(),
            "git": f"{branch} {sha}{dirty}",
            "python": platform.python_version(),
            "settings": rep.mask(_settings_text(inst.log_text())),
            "host_app": driver.host_app(),
            "permissions": perm_txt,
            "displays": str(displays),
            "input_mode": sc.input_mode,
            "ui_events": _ui_status_text(ui_status),
            "duration": f"{time.time() - started:.0f}s",
            "command": " ".join(["python", "scripts/e2e_collect.py"] + sys.argv[1:]),
        }
        path = STATUS_DIR / f"{args.status_name}.md"
        write_status(path, meta, rep.rows, [rep.mask(n) for n in sc.notes])
        summary = write_summary()
        log("")
        log(f"Wrote {path.relative_to(REPO)} and {summary.relative_to(REPO)}")
        log("Commit them:  git add docs/status && git commit -m \"docs(status): e2e run on "
            f"{args.status_name}\"")

    return 1 if rep.failed() else 0


if __name__ == "__main__":
    sys.exit(main())
