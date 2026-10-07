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

from screenmind.main import run  # noqa: E402

if __name__ == "__main__":
    run()
