"""Entry point for the frozen Evolve Capture app (PyInstaller spike).

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
    stop-evolve-capture.ps1) set the event Local\\EvolveCapture-Quit-<pid> instead.
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

    handle = kernel32.CreateEventW(None, True, False, f"Local\\EvolveCapture-Quit-{os.getpid()}")
    if not handle:
        return

    def wait():
        kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)  # INFINITE
        try:
            from evolve_capture.api import dependencies

            if dependencies.request_shutdown is not None:
                dependencies.request_shutdown()
                return
        except Exception:  # noqa: BLE001, S110
            pass
        os._exit(0)

    threading.Thread(target=wait, name="installer-quit", daemon=True).start()


def _self_test(image_path: str) -> int:
    """Build check: run both bundled models inside the app, print JSON.

    `EvolveCapture --self-test <image>`, used by packaging/macos/smoke_test.sh.
    It proves that onnxruntime and the bundled models load in the frozen app,
    with no screen access: OCR reads the image (F6), and the person-name
    model finds the name in a fixed line. Exit code 0 when both worked.
    """
    import json
    import time

    from PIL import Image

    from evolve_capture.engine import ocr
    from evolve_capture.privacy import person_ner

    start = time.monotonic()
    extractor = ocr.OCRExtractor()
    text = extractor.extract_text(Image.open(image_path))
    rec = extractor._rec_model
    ocr_seconds = time.monotonic() - start

    ner_dir = person_ner.find_model_dir()
    names = []
    if ner_dir is not None:
        line = "Meeting notes from Aleksandr Mangin"
        spans = person_ner.PersonNer(ner_dir).find([line])[0]
        names = [line[a:b] for a, b in spans]
    print(json.dumps({  # noqa: T201
        "text": text,
        "recognizer": rec,
        "models_dir": str(ocr._ocr_models_dir(rec)) if rec else None,
        "ocr_seconds": round(ocr_seconds, 2),
        "ner_model_dir": str(ner_dir) if ner_dir else None,
        "names": names,
    }))
    return 0 if text and names else 1


if sys.platform == "win32" and getattr(sys, "frozen", False):
    _watch_quit_event()

if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--self-test":
    sys.exit(_self_test(sys.argv[2]))

from evolve_capture.main import run  # noqa: E402

if __name__ == "__main__":
    run()
