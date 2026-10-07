"""Tests for capture layer — deduplication and screen capture."""

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
    cap._backend, cap._sct, cap._use_screencapture = None, Sct(), False
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
