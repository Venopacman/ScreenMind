"""Time ScreenCaptureKit grabs per display and compare them with `screencapture`.

macOS only. Run it from Terminal.app: processes started from Claude sessions
never get an SCK screenshot back. It saves nothing and does not touch the DB.

    .venv/bin/python scripts/sck-bench.py [rounds] [--mss]

--mss also times mss (CoreGraphics). That grab can hang for 30 s and stall
other apps that capture, so it is off by default.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mss  # noqa: E402
from PIL import ImageChops, ImageStat  # noqa: E402

from screenmind.capture import sck  # noqa: E402
from screenmind.capture.screen import _grab_screencapture  # noqa: E402


def say(text):
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def timed(fn, *args):
    start = time.monotonic()
    out = fn(*args)
    return out, time.monotonic() - start


def mean_diff(a, b):
    if a is None or b is None or a.size != b.size:
        return None
    return round(sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3, 2)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    rounds = int(args[0]) if args else 5
    with_mss = "--mss" in sys.argv

    say(f"python {sys.version.split()[0]}, SCK available: {sck.available()}")
    grabber = sck.SCKGrabber()
    sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
    for i, mon in enumerate(sct.monitors[1:], 1):
        say(f"\ndisplay {i}: {mon}")
        times = []
        img = None
        for _ in range(rounds):
            img, t = timed(grabber.grab, mon)
            times.append(t)
            if img is None:
                break
        status = "disabled" if grabber.disabled else ("ok" if img else "failed")
        size = img.size if img else None
        say(f"  SCK     {status:8} size={size} first={times[0]:.3f}s "
              f"median={sorted(times)[len(times) // 2]:.3f}s n={len(times)}")

        tool, t = timed(_grab_screencapture, mon)
        say(f"  tool    size={tool.size if tool else None} {t:.3f}s "
              f"mean |SCK-tool| per channel={mean_diff(img, tool)}")

        if with_mss:
            raw, t = timed(sct.grab, mon)
            from PIL import Image
            m = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
            say(f"  mss     size={m.size} {t:.3f}s mean |SCK-mss|={mean_diff(img, m)}")


if __name__ == "__main__":
    main()
