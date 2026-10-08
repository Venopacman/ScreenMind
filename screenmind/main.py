"""
ScreenMind — Main Entry Point
Starts all services: capture, analysis, API server.
Includes startup health checks and graceful error handling.
"""

import logging
import asyncio
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path

import uvicorn

from screenmind.config import is_frozen, settings, setup_file_log
from screenmind.storage.database import Database
from screenmind.workers.capture_worker import CaptureWorker
from screenmind.workers.analysis_worker import AnalysisWorker
from screenmind.workers.audio_worker import AudioWorker
from screenmind.workers.retention import Retention
from screenmind.api.server import create_app
from screenmind.watchdog import Watchdog, start_shutdown_deadline

logger = logging.getLogger("screenmind.main")


def check_llama_server() -> bool:
    """Check if llama-server is reachable and ready for inference."""
    from screenmind.engine import llm_client

    status = llm_client.get_server_status()
    if status["status"] == "ok":
        logger.info(f"OK - llama-server online at {settings.llama_server_host}")
        return True
    elif status["status"] == "unreachable":
        # Normal on a cold start: main() starts the server right after this.
        logger.info(f"llama-server is not running at {settings.llama_server_host} yet")
        return False
    else:
        logger.info(f"WARN - llama-server issue: {status['detail']}")
        return False


def check_disk_space():
    """Warn if disk space is low."""
    try:
        usage = shutil.disk_usage(str(settings.data_path))
        free_gb = usage.free / (1024 ** 3)
        if free_gb < 1.0:
            logger.info(f"WARN - Low disk space: {free_gb:.1f}GB free. ScreenMind needs space for screenshots.")
        else:
            logger.info(f"OK - Disk space: {free_gb:.1f}GB free")
    except Exception:
        pass


def print_first_run_help():
    """Show helpful info on first run (no DB yet)."""
    if not settings.db_path.exists():
        _safe_print()
        _safe_print("  +==========================================+")
        _safe_print("  |  Welcome to ScreenMind -- First Run!     |")
        _safe_print("  +==========================================+")
        _safe_print("  |  Screenshots will be saved to:           |")
        _safe_print(f"  |    {str(settings.screenshots_dir)[:38]:<38} |")
        _safe_print("  |                                          |")
        _safe_print("  |  Open the dashboard to see your timeline |")
        _safe_print("  +==========================================+")
        _safe_print()
        # Auto-install desktop shortcut on first run
        try:
            _install_desktop_shortcut()
        except Exception:
            pass  # Non-critical — don't block startup



def _is_interactive() -> bool:
    """Check if stdin is attached to a TTY (interactive terminal)."""
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False


def _is_port_in_use(port: int) -> bool:
    """Check if a port is already bound (another ScreenMind instance?)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _console_safe_stdout():
    """Never crash on a character the stdout encoding lacks.

    Redirected to a file or pipe on Windows, stdout uses the ANSI code page
    (cp1252), which has no box drawing, arrows or emoji, and its default
    errors="strict" raised UnicodeEncodeError at startup. stderr already
    uses backslashreplace.
    """
    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except (AttributeError, ValueError):
        pass  # devnull or a replaced stream without reconfigure()


def _safe_print(*args, **kwargs):
    """Print to stderr, but silently skip if stderr is None (pythonw.exe)."""
    if sys.stderr is not None:
        print(*args, file=sys.stderr, **kwargs)  # noqa: T201


async def main():
    """Initialize and run all ScreenMind services."""

    # ── Single-instance check (before any resource init) ──────────────
    if _is_port_in_use(settings.api_port):
        logger.error(f"Port {settings.api_port} already in use — is ScreenMind already running?")
        logger.error("If not, change api_port in settings or stop the other process.")
        sys.exit(1)

    _safe_print("=" * 60)
    _safe_print("  ScreenMind — Privacy-First Screen Activity Journal")
    _safe_print("  Powered by Gemma 4 E2B (100% Local)")
    _safe_print("=" * 60)
    _safe_print()

    # ── First-run experience ─────────────────────────────────────────
    settings.ensure_dirs()
    print_first_run_help()

    logger.info(f"Data directory: {settings.data_path}")
    logger.info("Settings that differ from the defaults: %s", settings.describe_non_defaults())
    logger.info(f"Capture interval: {settings.capture_interval}s")
    logger.info(f"Model: {settings.active_model}")
    if settings.blocked_apps_list:
        logger.info(f"Privacy zones: {', '.join(settings.blocked_apps_list)}")
    _safe_print()

    # ── llama-server setup ─────────────────────────────────────────────
    # Check if llama-server binary is available; offer to install if missing
    from screenmind.setup_llama import ensure_llama_server
    llama_binary_available = ensure_llama_server()

    # ── Health checks ────────────────────────────────────────────────
    from screenmind.engine import model_manager
    if llama_binary_available:
        # Binary exists — check if server is running, start if not
        if not check_llama_server():
            if not settings.llama_server_shared:  # shared: start_server only adopts
                logger.info("Starting llama-server automatically...")
            llm_server_ok = model_manager.start_server(settings.active_model, timeout=120)
        else:
            llm_server_ok = True
            # Detect what model the external server has loaded
            detection = model_manager.detect_running_model()
            if detection:
                model_manager.adopt_external_server(detection)
    else:
        llm_server_ok = False

    check_disk_space()
    if not llm_server_ok:
        _safe_print()
        logger.warning("Starting without Gemma 4 -- screenshots will be captured")
        logger.warning("but NOT analyzed until llama-server is available.")
        logger.warning("The dashboard and API will still work with existing data.")
        if not llama_binary_available and is_frozen():
            from screenmind.setup_llama import LLAMA_DIR
            logger.info(f"Put llama-server in {LLAMA_DIR} or on PATH to get analysis.")
        elif not llama_binary_available:
            logger.info("Run 'python -m screenmind.setup_llama' to install llama-server.")
        _safe_print()
    _safe_print()

    # ── Shared services ──────────────────────────────────────────────
    db = Database()
    # Fix any meetings left 'ongoing' from a previous crash
    stale = db.cleanup_stale_meetings()
    if stale:
        logger.info(f"Cleaned up {stale} stale meeting(s) from previous session")
    # Delete data older than retention_days now, and again each day (G24)
    retention = Retention(db)
    retention.run_if_due()

    # ── Processing queue ─────────────────────────────────────────────
    processing_queue: asyncio.Queue = asyncio.Queue(maxsize=100)

    # ── Workers ──────────────────────────────────────────────────────
    capture_worker = CaptureWorker(queue=processing_queue, database=db)
    analysis_worker = AnalysisWorker(queue=processing_queue, database=db)

    # ── Restore persisted capture state ──────────────────────────────
    if settings.capture_on_start:
        capture_worker.resume(source="capture_on_start")
        logger.info("Capture started (CAPTURE_ON_START).")
    elif not settings.capture_paused:
        capture_worker.resume(source="startup_restore")
        logger.info("Capture auto-resumed from previous session.")

    # ── Audio Worker (call tracking + meeting transcription) ─────────
    audio_worker = AudioWorker(database=db)
    audio_worker.start()  # own thread, independent of capture timing

    # ── UI Events (clicks, typing, app switches via accessibility APIs) ──
    from screenmind.capture.ui_events import UiEventRecorder, create_backend
    ui_recorder = UiEventRecorder(database=db, capture_worker=capture_worker, backend=create_backend())
    capture_worker._ui_recorder = ui_recorder

    # ── API Server ───────────────────────────────────────────────────
    app = create_app(
        database=db,
        capture_worker=capture_worker,
        analysis_worker=analysis_worker,
        audio_worker=audio_worker,
    )
    from screenmind.api import dependencies as _api_deps
    _api_deps.ui_recorder = ui_recorder

    # ── Graceful Shutdown ────────────────────────────────────────────
    shutdown_event = asyncio.Event()

    def _checkpoint_db():
        # Own connection: the main thread may be stuck holding its own
        import sqlite3
        conn = sqlite3.connect(str(settings.db_path), timeout=2)
        try:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        finally:
            conn.close()

    def start_deadline():
        # A thread stuck in an OS call must not keep the process alive
        start_shutdown_deadline(before_exit=_checkpoint_db)

    def handle_signal(*_):
        start_deadline()
        _safe_print("\n[Main] Shutdown signal received...")
        shutdown_event.set()

    def request_shutdown():
        start_deadline()  # here too: a stuck event loop would never run handle_signal
        _loop.call_soon_threadsafe(handle_signal)

    signal.signal(signal.SIGINT, handle_signal)
    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, handle_signal)
    # /api/shutdown runs in this event loop and uses the same path. A Ctrl+C
    # event to ourselves fails on Windows when there is no console (launcher,
    # pythonw): WinError 233, and the app keeps running.
    _loop = asyncio.get_running_loop()
    _api_deps.request_shutdown = request_shutdown

    # ── Safety check: never expose the API to the network ─────────────
    # The API has no auth. Binding to all interfaces would expose all
    # screen data to the network.
    if settings.api_host in ("0.0.0.0", "::"):
        _safe_print("")
        _safe_print("=" * 70)
        _safe_print("WARNING: The API has no auth. Binding to all interfaces would")
        _safe_print("   expose all screen data to your network.")
        _safe_print("   Falling back to 127.0.0.1 for safety.")
        _safe_print("=" * 70)
        _safe_print("")
        settings.api_host = "127.0.0.1"

    # ── Start API server in background thread ────────────────────────
    server_config = uvicorn.Config(
        app,
        host=settings.api_host,
        port=settings.api_port,
        log_level="warning",
    )
    server = uvicorn.Server(server_config)

    # Run uvicorn in a thread so it doesn't block the async loop
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    # ── Start Workers ────────────────────────────────────────────────
    ui_recorder.sync_with_settings()

    capture_task = asyncio.create_task(capture_worker.run())
    analysis_task = asyncio.create_task(analysis_worker.run())
    retention_task = asyncio.create_task(retention.run())

    # Logs where a stuck part waits; restarts a stuck UI-event enricher
    watchdog = Watchdog(capture_worker=capture_worker, analysis_worker=analysis_worker,
                        ui_recorder=ui_recorder, analysis_queue=processing_queue)
    watchdog.start()

    logger.info(f"Dashboard: http://{settings.api_host}:{settings.api_port}")
    logger.info(f"API docs:  http://{settings.api_host}:{settings.api_port}/docs")
    _safe_print()
    logger.info("ScreenMind is running! Press Ctrl+C to stop.")
    _safe_print()

    # ── Wait for shutdown ────────────────────────────────────────────
    await shutdown_event.wait()

    # ── Cleanup ──────────────────────────────────────────────────────
    logger.info("Shutting down...")
    watchdog.stop()
    capture_worker.stop()
    analysis_worker.stop()
    audio_worker.stop()
    ui_recorder.stop()
    server.should_exit = True

    capture_task.cancel()
    # Abandons a Gemma call in flight; its frame stays 'pending' for backfill
    analysis_task.cancel()
    retention_task.cancel()

    try:
        await asyncio.gather(capture_task, analysis_task, retention_task, return_exceptions=True)
    except asyncio.CancelledError:
        pass

    db.close()
    model_manager.stop_server()
    logger.info("Goodbye!")


def _install_desktop_shortcut() -> None:
    """Create a desktop shortcut for ScreenMind (cross-platform).

    Not in the app (frozen): the installer makes the shortcuts, and the
    launcher.vbs / launcher.py it would point at do not exist there.
    """
    if is_frozen():
        logger.info("Desktop shortcut skipped: the app's installer creates shortcuts")
        return
    desktop = Path.home() / "Desktop"
    if not desktop.exists():
        desktop = Path.home()

    launcher_py = Path(__file__).parent / "launcher.py"
    data_dir = settings.data_path
    data_dir.mkdir(parents=True, exist_ok=True)

    if sys.platform == "win32":
        # Create VBS wrapper in data dir (not package dir — avoids PermissionError)
        vbs_path = data_dir / "launcher.vbs"
        python_exe = sys.executable
        vbs_path.write_text(
            f'Set WshShell = CreateObject("WScript.Shell")\n'
            f'WshShell.Run """{python_exe}"" ""{launcher_py}""", 0, False\n',
            encoding="utf-8",
        )

        # Create .lnk shortcut
        try:
            shortcut_path = desktop / "ScreenMind.lnk"
            ps_cmd = (
                f'$s=(New-Object -COM WScript.Shell).CreateShortcut("{shortcut_path}");'
                f'$s.TargetPath="wscript.exe";'
                f'$s.Arguments="`"{vbs_path}`"";'
                f'$s.WorkingDirectory="{launcher_py.parent.parent}";'
                f'$s.Description="ScreenMind - Privacy-First AI Screen Journal";'
            )
            icon_path = Path(__file__).parent / "assets" / "favicon.ico"
            if icon_path.exists():
                ps_cmd += f'$s.IconLocation="{icon_path}";'
            ps_cmd += '$s.Save()'
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True,
            )
            print(f"[OK] Desktop shortcut created: {shortcut_path}")  # noqa: T201
        except Exception as e:
            print(f"[FAIL] Failed to create shortcut: {e}")  # noqa: T201

    elif sys.platform == "darwin":
        cmd_path = desktop / "ScreenMind.command"
        cmd_path.write_text(
            f'#!/bin/bash\n"{sys.executable}" "{launcher_py}"\n',
            encoding="utf-8",
        )
        os.chmod(str(cmd_path), 0o755)
        print(f"[OK] Desktop launcher created: {cmd_path}")  # noqa: T201

    else:
        desktop_file = desktop / "screenmind.desktop"
        desktop_file.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=ScreenMind\n"
            "Comment=Privacy-First AI Screen Journal\n"
            f'Exec="{sys.executable}" "{launcher_py}"\n'
            "Terminal=false\n"
            "Categories=Utility;\n",
            encoding="utf-8",
        )
        os.chmod(str(desktop_file), 0o755)
        print(f"[OK] Desktop launcher created: {desktop_file}")  # noqa: T201


def run():
    """Sync entry point for CLI: `screenmind` command."""
    _console_safe_stdout()
    # ── Exit-early flags (checked in order to prevent --background from swallowing them) ──
    if "--version" in sys.argv:
        from screenmind import __version__
        print(f"screenmind {__version__}")  # noqa: T201
        return
    if "--help" in sys.argv or "-h" in sys.argv:
        from screenmind import __version__
        print(f"ScreenMind {__version__} -- Privacy-First AI Screen Activity Journal")  # noqa: T201
        print()  # noqa: T201
        print("Usage: screenmind [OPTIONS]")  # noqa: T201
        print()  # noqa: T201
        print("Options:")  # noqa: T201
        print("  --version            Show version and exit")  # noqa: T201
        print("  --help, -h           Show this help and exit")  # noqa: T201
        print("  --background         Run silently without a console window")  # noqa: T201
        print("  --launch             Start with splash screen + open dashboard")  # noqa: T201
        print("  --install-startup    Register ScreenMind to start at system login")  # noqa: T201
        print("  --uninstall-startup  Remove ScreenMind from system startup")  # noqa: T201
        print("  --install-shortcut   Create a desktop shortcut")  # noqa: T201
        return

    # ── Startup registration (before --background to prevent swallowing) ──
    if "--install-startup" in sys.argv:
        from screenmind.startup import install_startup
        ok = install_startup()
        sys.exit(0 if ok else 1)
    if "--uninstall-startup" in sys.argv:
        from screenmind.startup import uninstall_startup
        ok = uninstall_startup()
        sys.exit(0 if ok else 1)

    # ── Desktop shortcut ──────────────────────────────────────────────
    if "--install-shortcut" in sys.argv:
        _install_desktop_shortcut()
        return
    if "--launch" in sys.argv:
        from screenmind.launcher import main as launch_main
        launch_main()
        return

    # ── Background mode: re-launch headless and exit ──
    if "--background" in sys.argv:
        # The child writes the log file itself (setup_file_log below)
        log_path = os.environ.get("SCREENMIND_LOG_FILE") or settings.data_path / "screenmind.log"

        if is_frozen():
            # The app is its own start command: no pythonw, no `-m screenmind`
            if sys.platform == "win32":
                subprocess.Popen(
                    [sys.executable],
                    stdin=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
                )
            else:
                subprocess.Popen(
                    [sys.executable],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
        elif sys.platform == "win32":
            # Try pythonw (no console window)
            pythonw = sys.executable.replace("python.exe", "pythonw.exe")
            if Path(pythonw).exists():
                subprocess.Popen(
                    [pythonw, "-m", "screenmind"],
                    stdin=subprocess.DEVNULL,
                )
            else:
                # Fallback: CREATE_NO_WINDOW with regular python
                subprocess.Popen(
                    [sys.executable, "-m", "screenmind"],
                    stdin=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
                )
        else:
            # macOS/Linux: detach from terminal
            subprocess.Popen(
                [sys.executable, "-m", "screenmind"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

        print(f"ScreenMind started in background. Logs: {log_path}")  # noqa: T201
        print(f"Dashboard: http://{settings.api_host}:{settings.api_port}")  # noqa: T201
        return

    # Every app start logs to a rotating file, console or not (G38)
    log_path = setup_file_log(settings.data_path)
    from screenmind import __version__
    logger.info("ScreenMind %s starting: pid %d, Python %s, %s. Log file: %s",
                __version__, os.getpid(), sys.version.split()[0], sys.platform, log_path)
    try:
        asyncio.run(main())
    except Exception:
        logger.exception("ScreenMind stopped on an error")
        raise


if __name__ == "__main__":
    run()
