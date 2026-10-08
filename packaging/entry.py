"""Entry point for the frozen ScreenMind app (PyInstaller spike).

See docs/plans/packaging.md and docs/plans/packaging-spikes.md.
"""
import os
import sys

# A windowed build (console=False) has no stdin/stdout/stderr on Windows.
# uvicorn and logging call .isatty()/.write() on them, so point them at devnull.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
if sys.stdin is None:
    sys.stdin = open(os.devnull, "r", encoding="utf-8")  # noqa: SIM115


def _watch_quit_event():
    """Windows: stop cleanly when the installer asks.

    The windowed app has no console and no window, so Ctrl+C and WM_CLOSE
    cannot reach it. The installer and uninstaller (packaging/windows/
    stop-screenmind.ps1) set the event Local\\ScreenMind-Quit-<pid> instead.
    A daemon thread blocks on it (no CPU). On the signal it runs the same
    clean stop as /api/shutdown, or exits at once if startup is not done yet.
    """
    import ctypes
    import threading
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

    handle = kernel32.CreateEventW(None, True, False, f"Local\\ScreenMind-Quit-{os.getpid()}")
    if not handle:
        return

    def wait():
        kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)  # INFINITE
        try:
            from screenmind.api import dependencies

            if dependencies.request_shutdown is not None:
                dependencies.request_shutdown()
                return
        except Exception:  # noqa: BLE001, S110
            pass
        os._exit(0)

    threading.Thread(target=wait, name="installer-quit", daemon=True).start()


if sys.platform == "win32" and getattr(sys, "frozen", False):
    _watch_quit_event()

from screenmind.main import run  # noqa: E402

if __name__ == "__main__":
    run()
