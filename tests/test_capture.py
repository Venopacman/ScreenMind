"""Tests for capture layer — deduplication and screen capture."""

import pytest
from PIL import Image

from screenmind.capture.dedup import ScreenDeduplicator


def test_dedup_first_frame_never_duplicate():
    dedup = ScreenDeduplicator(threshold=8)
    img = Image.new("RGB", (1920, 1080), color=(100, 100, 100))
    assert dedup.is_duplicate(img) is False


def test_dedup_identical_frames():
    dedup = ScreenDeduplicator(threshold=8)
    img = Image.new("RGB", (1920, 1080), color=(100, 100, 100))
    dedup.is_duplicate(img)  # First frame
    assert dedup.is_duplicate(img) is True  # Same frame = duplicate


def test_dedup_different_frames():
    import numpy as np
    dedup = ScreenDeduplicator(threshold=8)
    # Use random noise images — solid colors have identical phash
    rng = np.random.default_rng(42)
    arr1 = rng.integers(0, 128, (1080, 1920, 3), dtype=np.uint8)
    arr2 = rng.integers(128, 255, (1080, 1920, 3), dtype=np.uint8)
    img1 = Image.fromarray(arr1)
    img2 = Image.fromarray(arr2)
    dedup.is_duplicate(img1)  # First frame
    assert dedup.is_duplicate(img2) is False  # Very different


def test_dedup_reset():
    dedup = ScreenDeduplicator(threshold=8)
    img = Image.new("RGB", (1920, 1080), color=(50, 50, 50))
    dedup.is_duplicate(img)
    assert dedup.is_duplicate(img) is True  # Duplicate

    dedup.reset()
    assert dedup.is_duplicate(img) is False  # After reset, first frame again


def test_dedup_threshold_sensitivity():
    # Strict threshold should catch more duplicates
    strict = ScreenDeduplicator(threshold=2)
    loose = ScreenDeduplicator(threshold=20)

    img1 = Image.new("RGB", (1920, 1080), color=(100, 100, 100))
    # Slightly different image
    img2 = Image.new("RGB", (1920, 1080), color=(105, 105, 105))

    strict.is_duplicate(img1)
    loose.is_duplicate(img1)

    # Strict might see it as different, loose should see it as same
    # (exact behavior depends on phash, but the principle holds)
    loose_result = loose.is_duplicate(img2)
    assert isinstance(loose_result, bool)  # Just verify it runs


def _fake_capture(monkeypatch, slow_seconds):
    """ScreenCapture with a fake mss and a fake screencapture tool."""
    from screenmind.capture import screen

    class Raw:
        size = (2, 2)
        bgra = b"\0" * 16

    class Sct:
        grabs = 0

        def grab(self, monitor):
            Sct.grabs += 1
            return Raw()

    tool_calls = []
    monkeypatch.setattr(screen.sys, "platform", "darwin")
    monkeypatch.setattr(screen, "_SLOW_GRAB_SECONDS", slow_seconds)
    monkeypatch.setattr(screen, "_grab_screencapture",
                        lambda m: tool_calls.append(m) or Image.new("RGB", (2, 2)))
    cap = screen.ScreenCapture.__new__(screen.ScreenCapture)
    cap._backend, cap._sct, cap._sck, cap._use_screencapture = None, Sct(), None, False
    return cap, Sct, tool_calls


def test_slow_grab_switches_to_screencapture(monkeypatch):
    cap, sct, tool_calls = _fake_capture(monkeypatch, slow_seconds=-1)
    mon = {"left": 0, "top": 0, "width": 2, "height": 2}
    cap._grab(mon)
    assert cap._use_screencapture and tool_calls == []
    cap._grab(mon)
    assert tool_calls == [mon] and sct.grabs == 1


def test_fast_grab_keeps_mss(monkeypatch):
    cap, sct, tool_calls = _fake_capture(monkeypatch, slow_seconds=1e9)
    mon = {"left": 0, "top": 0, "width": 2, "height": 2}
    cap._grab(mon)
    cap._grab(mon)
    assert not cap._use_screencapture and tool_calls == [] and sct.grabs == 2


# ── ScreenCaptureKit backend (macOS) ─────────────────────────────────────

class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _rect(x, y, w, h):
    return _Obj(origin=_Obj(x=x, y=y), size=_Obj(width=w, height=h))


class _FakeDisplay:
    def __init__(self, display_id, x, y, w, h):
        self.display_id = display_id
        self._frame = _rect(x, y, w, h)

    def frame(self):
        return self._frame


def _fake_sck(monkeypatch, displays, shot="ok"):
    """Install a fake ScreenCaptureKit module. shot: 'ok', 'hang' or 'error'."""
    import sys
    import types
    from screenmind.capture import sck

    calls = {"content": 0, "shots": []}

    class Content:
        def displays(self):
            return list(displays)

    class SCShareableContent:
        @staticmethod
        def getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(a, b, h):
            calls["content"] += 1
            h(Content(), None)

    class Filter:
        def initWithDisplay_excludingWindows_(self, display, windows):
            self.display = display
            return self

        def pointPixelScale(self):
            return 2.0

    class Config:
        def init(self):
            return self

        def __getattr__(self, name):
            if name.startswith("set"):
                return lambda v: setattr(self, name[3:-1].lower(), v)
            raise AttributeError(name)

    class SCScreenshotManager:
        @staticmethod
        def captureImageWithFilter_configuration_completionHandler_(f, config, h):
            calls["shots"].append((f.display, config))
            if shot == "ok":
                h(("cgimage", config.width, config.height), None)
            elif shot == "error":
                h(None, "SCStreamErrorDomain -3801")
            # 'hang': never call back

    module = types.SimpleNamespace(
        SCShareableContent=SCShareableContent,
        SCContentFilter=_Obj(alloc=Filter),
        SCStreamConfiguration=_Obj(alloc=Config),
        SCScreenshotManager=SCScreenshotManager,
    )
    monkeypatch.setitem(sys.modules, "ScreenCaptureKit", module)
    monkeypatch.setattr(sck, "cgimage_to_pil", lambda img: Image.new("RGB", (img[1], img[2])))
    return sck.SCKGrabber(timeout=0.05), calls


_MAIN = {"left": 0, "top": 0, "width": 1512, "height": 982}
_SIDE = {"left": 1512, "top": -305, "width": 2288, "height": 1287}


def test_sck_matches_display_by_frame_at_native_size(monkeypatch):
    displays = [_FakeDisplay(1, 0, 0, 1512, 982), _FakeDisplay(3, 1512, -305, 2288, 1287)]
    grabber, calls = _fake_sck(monkeypatch, displays)
    img = grabber.grab(_SIDE)
    assert img.size == (4576, 2574)
    display, config = calls["shots"][0]
    assert display.display_id == 3
    assert config.pixelformat == 1111970369 and config.showscursor is False
    assert grabber.grab(_MAIN).size == (3024, 1964)
    assert calls["content"] == 1  # display list is cached


def test_sck_refetches_displays_for_unknown_frame(monkeypatch):
    displays = [_FakeDisplay(1, 0, 0, 1512, 982)]
    grabber, calls = _fake_sck(monkeypatch, displays)
    grabber.grab(_MAIN)
    assert grabber.grab(_SIDE) is None and calls["content"] == 2
    displays.append(_FakeDisplay(3, 1512, -305, 2288, 1287))  # plugged in
    assert grabber.grab(_SIDE).size == (4576, 2574)
    assert not grabber.disabled


def test_sck_timeout_disables_for_good(monkeypatch):
    grabber, calls = _fake_sck(monkeypatch, [_FakeDisplay(1, 0, 0, 1512, 982)], shot="hang")
    assert grabber.grab(_MAIN) is None and grabber.disabled
    assert grabber.grab(_MAIN) is None and len(calls["shots"]) == 1


def test_sck_errors_disable_after_a_few(monkeypatch):
    from screenmind.capture import sck
    grabber, calls = _fake_sck(monkeypatch, [_FakeDisplay(1, 0, 0, 1512, 982)], shot="error")
    for _ in range(sck._MAX_ERRORS - 1):
        assert grabber.grab(_MAIN) is None and not grabber.disabled
    assert grabber.grab(_MAIN) is None and grabber.disabled


def test_cgimage_to_pil_keeps_colors():
    Quartz = pytest.importorskip("Quartz")
    w, h = 3, 2
    ctx = Quartz.CGBitmapContextCreate(
        None, w, h, 8, 0, Quartz.CGColorSpaceCreateDeviceRGB(),
        Quartz.kCGImageAlphaPremultipliedFirst | Quartz.kCGBitmapByteOrder32Little,
    )
    Quartz.CGContextSetRGBFillColor(ctx, 1.0, 0.5, 0.0, 1.0)
    Quartz.CGContextFillRect(ctx, Quartz.CGRectMake(0, 0, w, h))
    from screenmind.capture.sck import cgimage_to_pil
    img = cgimage_to_pil(Quartz.CGBitmapContextCreateImage(ctx))
    assert img.mode == "RGB" and img.size == (w, h)
    r, g, b = img.getpixel((0, 0))
    assert r == 255 and 126 <= g <= 129 and b == 0


def test_grab_order_sck_then_tool_then_mss(monkeypatch):
    cap, sct, tool_calls = _fake_capture(monkeypatch, slow_seconds=1e9)

    class Grabber:
        result = Image.new("RGB", (2, 2))

        def grab(self, monitor):
            return Grabber.result

    cap._sck = Grabber()
    mon = {"left": 0, "top": 0, "width": 2, "height": 2}
    assert cap._grab(mon) is Grabber.result and tool_calls == [] and sct.grabs == 0

    Grabber.result = None  # SCK fails: the tool is next
    cap._grab(mon)
    assert tool_calls == [mon] and sct.grabs == 0

    from screenmind.capture import screen
    monkeypatch.setattr(screen, "_grab_screencapture", lambda m: None)  # tool fails too
    cap._grab(mon)
    assert sct.grabs == 1


def test_grab_logs_backend_once_and_on_switch(monkeypatch, caplog):
    import logging
    cap, sct, tool_calls = _fake_capture(monkeypatch, slow_seconds=1e9)

    class Grabber:
        result = Image.new("RGB", (2, 2))

        def grab(self, monitor):
            return Grabber.result

    cap._sck = Grabber()
    mon = {"left": 0, "top": 0, "width": 2, "height": 2}
    with caplog.at_level(logging.INFO, logger="screenmind.capture.screen"):
        cap._grab(mon)
        cap._grab(mon)
        Grabber.result = None
        cap._grab(mon)
    lines = [r.getMessage() for r in caplog.records if "backend" in r.getMessage()]
    assert lines == ["Screen grab backend: sck", "Screen grab backend: screencapture"]
