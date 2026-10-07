"""
macOS screen grabs with ScreenCaptureKit (SCScreenshotManager, macOS 14+).

CGWindowListCreateImage (what mss uses) is deprecated and can hang for 30 s.
SCK is Apple's replacement. Its API is async, so this module waits on the
completion handler with a timeout and gives up on SCK for good after a stuck
call: from processes started by Claude sessions the screenshot handler never
fires, and a stuck request may hold up other apps that capture.
"""

import logging
import threading
import time
from typing import Optional

from PIL import Image

logger = logging.getLogger("screenmind.capture.sck")

_TIMEOUT_SECONDS = 3.0
# Display list is cheap to fetch (~15 ms); refresh it now and then so a
# replugged display with the same frame doesn't keep a stale SCDisplay.
_CONTENT_MAX_AGE_SECONDS = 60.0
# Errors from the handler (not timeouts) before SCK is dropped for good
_MAX_ERRORS = 3

_BGRA = 1111970369  # kCVPixelFormatType_32BGRA; SCK defaults to YUV 420v
_BYTE_ORDER_32_LITTLE = 2 << 12  # kCGBitmapByteOrder32Little
_BYTE_ORDER_MASK = 7 << 12


class SCKTimeout(Exception):
    pass


def available() -> bool:
    """True when pyobjc's ScreenCaptureKit has SCScreenshotManager (macOS 14+)."""
    try:
        import ScreenCaptureKit
        return hasattr(ScreenCaptureKit, "SCScreenshotManager")
    except Exception:
        return False


def _await(start, timeout: float):
    """Call start(handler) and wait for handler(result, error). Raises on
    timeout or error. Each call has its own box, so a late handler is harmless."""
    done = threading.Event()
    box = {}

    def handler(result, error):
        box["result"], box["error"] = result, error
        done.set()

    start(handler)
    if not done.wait(timeout):
        raise SCKTimeout(f"no answer in {timeout:.0f}s")
    if box["error"] is not None:
        raise RuntimeError(str(box["error"]))
    return box["result"]


def _frame_key(x, y, w, h) -> tuple:
    return round(x), round(y), round(w), round(h)


def cgimage_to_pil(cgimage) -> Image.Image:
    """32-bit BGRA CGImage (what SCK returns with _BGRA) to an RGB PIL image."""
    import Quartz

    width = Quartz.CGImageGetWidth(cgimage)
    height = Quartz.CGImageGetHeight(cgimage)
    bpp = Quartz.CGImageGetBitsPerPixel(cgimage)
    order = Quartz.CGImageGetBitmapInfo(cgimage) & _BYTE_ORDER_MASK
    if bpp != 32 or order != _BYTE_ORDER_32_LITTLE:
        raise ValueError(f"unexpected pixel layout: {bpp} bpp, byte order {order}")
    stride = Quartz.CGImageGetBytesPerRow(cgimage)
    data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(cgimage))
    return Image.frombuffer("RGB", (width, height), bytes(data), "raw", "BGRX", stride, 1)


class SCKGrabber:
    """Grabs one display (an mss monitor dict in global points) at native resolution."""

    def __init__(self, timeout: float = _TIMEOUT_SECONDS):
        # Quartz must be loaded before the first screenshot handler runs.
        # Otherwise pyobjc can't type the CGImageRef and passes a raw pointer.
        import Quartz  # noqa: F401
        import ScreenCaptureKit

        self._sck = ScreenCaptureKit
        self._timeout = timeout
        self._displays = {}  # frame key -> SCDisplay
        self._fetched_at = 0.0
        self._errors = 0
        self.disabled = False

    def grab(self, monitor: dict) -> Optional[Image.Image]:
        """RGB image of the display, or None so the caller falls back."""
        if self.disabled:
            return None
        try:
            display = self._display_for(monitor)
            if display is None:
                logger.debug("No SCK display at %s", monitor)
                return None
            img = cgimage_to_pil(self._screenshot(display))
        except SCKTimeout as e:
            self._disable(f"timed out ({e})")
            return None
        except Exception as e:
            self._errors += 1
            self._displays = {}  # maybe stale; refetch next time
            if self._errors >= _MAX_ERRORS:
                self._disable(f"failed {self._errors} times in a row ({e})")
            else:
                logger.debug("SCK grab failed: %s", e)
            return None
        self._errors = 0
        return img

    def _disable(self, reason: str):
        self.disabled = True
        logger.warning("ScreenCaptureKit %s; using the screencapture tool from now on", reason)

    def _display_for(self, monitor: dict):
        key = _frame_key(monitor["left"], monitor["top"], monitor["width"], monitor["height"])
        stale = time.monotonic() - self._fetched_at > _CONTENT_MAX_AGE_SECONDS
        if stale or key not in self._displays:
            self._refresh()
        return self._displays.get(key)

    def _refresh(self):
        content = _await(
            lambda h: self._sck.SCShareableContent
            .getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
                False, True, h),
            self._timeout,
        )
        displays = {}
        for d in content.displays():
            f = d.frame()
            displays[_frame_key(f.origin.x, f.origin.y, f.size.width, f.size.height)] = d
        self._displays = displays
        self._fetched_at = time.monotonic()

    def _screenshot(self, display):
        sck = self._sck
        content_filter = sck.SCContentFilter.alloc().initWithDisplay_excludingWindows_(display, [])
        scale = content_filter.pointPixelScale()
        frame = display.frame()
        config = sck.SCStreamConfiguration.alloc().init()
        config.setWidth_(round(frame.size.width * scale))
        config.setHeight_(round(frame.size.height * scale))
        config.setPixelFormat_(_BGRA)
        config.setShowsCursor_(False)  # mss and `screencapture -x` leave it out too
        return _await(
            lambda h: sck.SCScreenshotManager
            .captureImageWithFilter_configuration_completionHandler_(content_filter, config, h),
            self._timeout,
        )
