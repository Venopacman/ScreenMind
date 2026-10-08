"""
ScreenMind Launcher
Shows a splash screen while ScreenMind starts in the background.
Once the server is ready, opens the dashboard in the default browser.

Cross-platform: Windows (tkinter), macOS/Linux (terminal poll fallback).

Usage:
    python screenmind/launcher.py       # show splash + start
    Via VBS wrapper on Windows for no-console experience.
"""

import os
import sys
import subprocess
import time
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────
DASHBOARD_URL = "http://127.0.0.1:7777"
POLL_INTERVAL = 2
MAX_WAIT = 120


def _open_in_browser(url: str) -> None:
    """Open a URL in the default browser (cross-platform)."""
    if sys.platform == "win32":
        os.startfile(url)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", url])  # noqa: S603
    else:
        subprocess.Popen(["xdg-open", url])  # noqa: S603


def is_server_running() -> bool:
    """Check if ScreenMind dashboard is responding."""
    try:
        from urllib.request import urlopen
        r = urlopen(DASHBOARD_URL, timeout=3)
        return r.status == 200
    except Exception:
        return False


def start_screenmind() -> None:
    """Launch ScreenMind in the background.

    Its output goes to DEVNULL. ScreenMind writes its own rotating log,
    screenmind.log in the data dir (main.run()).
    """
    if sys.platform == "win32":
        pythonw = sys.executable.replace("python.exe", "pythonw.exe")
        if not Path(pythonw).exists():
            pythonw = sys.executable
        CREATE_NO_WINDOW = 0x08000000
        subprocess.Popen(
            [pythonw, "-m", "screenmind"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
    else:
        subprocess.Popen(
            [sys.executable, "-m", "screenmind"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def _run_with_splash() -> None:
    """Show a tkinter splash screen while polling for server readiness."""
    import threading
    try:
        import tkinter as tk
        from tkinter import font as tkfont
    except ImportError:
        # No tkinter — fall back to headless poll
        _run_headless()
        return

    root = tk.Tk()
    root.title("ScreenMind")
    root.configure(bg="#0d0d1a")
    root.resizable(False, False)
    root.attributes("-topmost", True)

    # Center on screen
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    w, h = 340, 380
    x, y = (sw - w) // 2, (sh - h) // 2
    root.geometry(f"{w}x{h}+{x}+{y}")

    # Try to load logo
    logo_path = Path(__file__).parent / "assets" / "logo.png"
    if logo_path.exists():
        try:
            logo_img = tk.PhotoImage(file=str(logo_path))
            factor = max(1, logo_img.width() // 140)
            if factor > 1:
                logo_img = logo_img.subsample(factor, factor)
            logo_label = tk.Label(root, image=logo_img, bg="#0d0d1a")
            logo_label.image = logo_img
            logo_label.pack(pady=(45, 12))
        except Exception:
            tk.Label(root, text="🧠", font=("Segoe UI Emoji", 52), bg="#0d0d1a").pack(pady=(35, 8))
    else:
        tk.Label(root, text="🧠", font=("Segoe UI Emoji", 52), bg="#0d0d1a").pack(pady=(35, 8))

    # Title
    try:
        title_font = tkfont.Font(family="Segoe UI", size=20, weight="bold")
    except Exception:
        title_font = ("Arial", 20, "bold")
    tk.Label(root, text="ScreenMind", font=title_font, fg="#a78bfa", bg="#0d0d1a").pack(pady=(0, 4))
    tk.Label(root, text="Privacy-First AI Screen Journal", font=("Segoe UI", 9), fg="#6b7280", bg="#0d0d1a").pack()

    # Status
    status_var = tk.StringVar(value="Starting...")
    status_label = tk.Label(root, textvariable=status_var, font=("Segoe UI", 10), fg="#9ca3af", bg="#0d0d1a")
    status_label.pack(pady=(28, 0))

    # Dot animation
    dot_count = [0]
    def animate():
        dot_count[0] = (dot_count[0] % 3) + 1
        txt = status_var.get()
        base = txt.rstrip(".")
        if base in ("Starting", "Loading AI models", "Waiting for server") or base.startswith("Still loading"):
            status_var.set(base + "." * dot_count[0])
        if root.winfo_exists():
            root.after(500, animate)
    animate()

    # Force to front
    root.lift()
    root.focus_force()
    root.update()

    start_time = time.time()
    server_started = [False]

    def poll():
        """Background thread: poll server readiness."""
        while time.time() - start_time < MAX_WAIT:
            if is_server_running():
                server_started[0] = True
                return
            time.sleep(POLL_INTERVAL)

    poll_thread = threading.Thread(target=poll, daemon=True)
    poll_thread.start()

    def check_result():
        elapsed = time.time() - start_time
        if server_started[0]:
            status_var.set("Ready! ✓")
            status_label.config(fg="#34d399")
            root.update()
            root.after(500, lambda: _finish(root))
            return
        if elapsed > MAX_WAIT:
            status_var.set("Timeout — opening anyway...")
            root.after(1500, lambda: _finish(root))
            return
        # Update status text
        if elapsed < 5:
            status_var.set("Starting")
        elif elapsed < 15:
            status_var.set("Loading AI models")
        elif elapsed < 45:
            status_var.set("Waiting for server")
        else:
            status_var.set(f"Still loading ({int(elapsed)}s)")
        root.after(1000, check_result)

    root.after(2000, check_result)

    # Window icon
    icon_path = Path(__file__).parent / "assets" / "favicon.ico"
    if icon_path.exists():
        try:
            root.iconbitmap(str(icon_path))
        except Exception:
            pass

    root.mainloop()


def _finish(root) -> None:
    """Open browser and close splash."""
    _open_in_browser(DASHBOARD_URL)
    try:
        root.destroy()
    except Exception:
        pass


def _run_headless() -> None:
    """Fallback: poll without GUI, open browser when ready."""
    start = time.time()
    while time.time() - start < MAX_WAIT:
        time.sleep(POLL_INTERVAL)
        if is_server_running():
            _open_in_browser(DASHBOARD_URL)
            return
    _open_in_browser(DASHBOARD_URL)


def main() -> None:
    """Entry point."""
    if is_server_running():
        _open_in_browser(DASHBOARD_URL)
        return

    # Prevent double-click race: if another launcher is already starting
    lock_path = Path.home() / ".screenmind" / ".launcher.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.exists():
        # Check if lock is stale (older than MAX_WAIT seconds)
        try:
            age = time.time() - lock_path.stat().st_mtime
            if age < MAX_WAIT:
                _open_in_browser(DASHBOARD_URL)  # another launcher is running
                return
        except OSError:
            pass
    lock_path.touch()

    try:
        start_screenmind()
        _run_with_splash()
    finally:
        lock_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
