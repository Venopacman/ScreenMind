"""Allow `python -m screenmind` to start the application."""
import os
import sys

# ── pythonw.exe compatibility ─────────────────────────────────────────────
# Under pythonw.exe (Windows GUI mode), sys.stdin/stdout/stderr are None.
# Many libraries (uvicorn, logging, etc.) call .isatty() or .write() on them
# and crash with AttributeError. Redirect to devnull before any imports.
# The log file does not depend on this: main.run() always writes one.
_no_console = sys.stderr is None
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
if sys.stdin is None:
    sys.stdin = open(os.devnull, "r", encoding="utf-8")  # noqa: SIM115

# ── Crash wrapper ─────────────────────────────────────────────────────────
# Without a console (pythonw.exe, a detached python.exe), unhandled exceptions
# vanish silently. main.run() logs those after startup to screenmind.log; this
# also catches import errors, which come before the log file exists.
if _no_console:
    import traceback
    _data = os.environ.get(
        "SCREENMIND_DATA_DIR",
        os.path.join(os.path.expanduser("~"), ".screenmind"),
    )
    os.makedirs(_data, exist_ok=True)
    _crash_log = os.path.join(_data, "crash.log")
    try:
        from screenmind.main import run
        run()
    except SystemExit:
        pass  # Normal exit
    except Exception:
        with open(_crash_log, "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
else:
    from screenmind.main import run
    run()
