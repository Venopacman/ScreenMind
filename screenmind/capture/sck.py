"""
macOS screen grabs with ScreenCaptureKit (SCScreenshotManager, macOS 14+).

CGWindowListCreateImage (what mss uses) is deprecated and can hang for 30 s.
SCK is Apple's replacement. Its API is async, so this module waits on the
completion handler with a timeout. After a timeout (or a few errors) the caller
uses the screencapture tool for a cool-down, then one probe checks whether SCK
answers again. Probes never block the capture tick. From processes started by
Claude sessions the screenshot handler never fires, so there SCK stops for good
after a couple of failed probes.
"""

import logging
import threading
import time
from datetime import datetime
from typing import Optional

from PIL import Image

logger = logging.getLogger("screenmind.capture.sck")

_TIMEOUT_SECONDS = 3.0
# Display list is cheap to fetch (~15 ms); refresh it now and then so a
# replugged display with the same frame doesn't keep a stale SCDisplay.
_CONTENT_MAX_AGE_SECONDS = 60.0
# Errors from the handler (not timeouts) in a row before SCK gets a cool-down
_MAX_ERRORS = 3
# Cool-downs after a fallback, one probe after each. The last one repeats.
_RETRY_MINUTES = (5, 10, 20, 40, 60)
# The back-off starts over once SCK has worked this long after coming back
_STABLE_SECONDS = 30 * 60
# Failed probes before SCK stops for good, when it never gave a frame in this
# process (started from a Claude session: the handler never fires there)
_MAX_PROBES_NEVER_WORKED = 2
# A probe fetches the display list in the capture tick; keep that wait short
_PROBE_CONTENT_TIMEOUT = 1.0

_BGRA = 1111970369  # kCVPixelFormatType_32BGRA; SCK defaults to YUV 420v
_BYTE_ORDER_32_LITTLE = 2 << 12  # kCGBitmapByteOrder32Little
_BYTE_ORDER_MASK = 7 << 12

_clock = time.monotonic  # tests replace it


class SCKTimeout(Exception):
    def __init__(self, message: str, request: "_Request"):
        super().__init__(message)
        self.request = request


def available() -> bool:
    """True when pyobjc's ScreenCaptureKit has SCScreenshotManager (macOS 14+)."""
    try:
        import ScreenCaptureKit
        return hasattr(ScreenCaptureKit, "SCScreenshotManager")
    except Exception:
        return False


class _Request:
    """One SCK call. Its handler may fire late, or never.

    Holds no thread: the handler runs on an SCK queue. A request that never
    answers costs a few KB (200 of them added ~4 MB, no threads; 2026-10-08).
    """

    def __init__(self, keep: bool = True):
        self.done = threading.Event()
        self.started = _clock()
        self.answered: Optional[float] = None
        self.result = self.error = None
        self.keep = keep  # False: drop the result, only the answer matters

    def handler(self, result, error):
        if self.keep:
            self.result = result
        self.error = error
        self.answered = _clock()
        self.done.set()

    def drop(self):
        """The caller gave up. A late image (tens of MB) is not kept."""
        self.keep = False
        self.result = None


def _start(start, keep: bool = True) -> _Request:
    """Call start(handler) and return at once."""
    request = _Request(keep)
    start(request.handler)
    return request


def _await(start, timeout: float):
    """Call start(handler) and wait for handler(result, error). Raises on
    timeout or error."""
    request = _start(start)
    if not request.done.wait(timeout):
        request.drop()
        raise SCKTimeout(f"no answer in {timeout:.0f}s", request)
    if request.error is not None:
        raise RuntimeError(str(request.error))
    return request.result


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
    """Grabs one display (an mss monitor dict in global points) at native resolution.

    States: "on" (grab() uses SCK), "cooldown" (grab() returns None and the
    caller uses the screencapture tool; one probe goes out when the cool-down
    ends) and "off" (for good). At most one SCK request is waited for at a time.
    """

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
        self._worked = False  # SCK gave a frame in this process
        self._retry_at: Optional[float] = None  # set while in a cool-down
        self._level = 0  # index into _RETRY_MINUTES
        self._probes_failed = 0  # since the last fallback
        self._back_at: Optional[float] = None  # when a probe brought SCK back
        self._inflight: Optional[_Request] = None  # gave up on it or probing
        self._probe: Optional[_Request] = None
        self.off = False
        self.lost = 0  # requests still unanswered when the next one went out

    @property
    def disabled(self) -> bool:
        """True while grab() returns None: in a cool-down or off for good."""
        return self.off or self._retry_at is not None

    def grab(self, monitor: dict) -> Optional[Image.Image]:
        """RGB image of the display, or None so the caller falls back."""
        if self.off:
            return None
        if self._retry_at is not None and not self._probe_says_back(monitor):
            return None
        try:
            display = self._display_for(monitor, self._timeout)
            if display is None:
                logger.debug("No SCK display at %s", monitor)
                return None
            img = cgimage_to_pil(_await(lambda h: self._start_screenshot(display, h),
                                        self._timeout))
        except SCKTimeout as e:
            self._inflight = e.request
            self._fall_back(f"timed out ({e})")
            return None
        except Exception as e:
            self._errors += 1
            self._displays = {}  # maybe stale; refetch next time
            if self._errors >= _MAX_ERRORS:
                self._fall_back(f"failed {self._errors} times in a row ({e})")
            else:
                logger.debug("SCK grab failed: %s", e)
            return None
        self._errors = 0
        self._worked = True
        if self._back_at is not None and _clock() - self._back_at >= _STABLE_SECONDS:
            self._level, self._back_at = 0, None
        return img

    def status(self) -> dict:
        """For /api/status: state, next probe time (local ISO) and lost requests."""
        state = "off" if self.off else ("cooldown" if self._retry_at is not None else "on")
        next_retry = None
        if self._retry_at is not None and self._probe is None:
            wall = time.time() + max(0.0, self._retry_at - _clock())
            next_retry = datetime.fromtimestamp(wall).isoformat(timespec="seconds")
        return {
            "state": state,
            "next_retry": next_retry,
            "probing": self._probe is not None,
            "lost_requests": self.lost,
        }

    # ── Cool-down and probes ─────────────────────────────────────────────

    def _fall_back(self, reason: str):
        if self._back_at is not None and _clock() - self._back_at < _STABLE_SECONDS:
            self._level = min(self._level + 1, len(_RETRY_MINUTES) - 1)  # back too soon
        else:
            self._level = 0
        self._back_at = None
        self._errors = 0
        self._probes_failed = 0
        self._schedule(f"ScreenCaptureKit {reason}", logging.WARNING)

    def _schedule(self, reason: str, level: int):
        minutes = _RETRY_MINUTES[self._level]
        self._retry_at = _clock() + minutes * 60
        logger.log(level, "%s; using the screencapture tool, next try in %d min",
                   reason, minutes)

    def _probe_says_back(self, monitor: dict) -> bool:
        """In a cool-down: check the probe, or send one when the cool-down is over.
        True when SCK answered the probe in time and grab() may use it again."""
        probe = self._probe
        if probe is not None:
            if probe.done.is_set():
                self._probe = None
                took = probe.answered - probe.started
                if probe.error is None and took <= self._timeout:
                    self._come_back(took)
                    return True
                if probe.error is not None:
                    self._displays = {}  # maybe stale; the next probe refetches
                    self._probe_failed(f"failed ({probe.error})")
                else:
                    self._probe_failed(f"answered too late ({took:.0f}s)")
            elif _clock() - probe.started > self._timeout:
                self._probe = None  # stays in _inflight until the next probe
                self._probe_failed(f"got no answer in {self._timeout:.0f}s")
            return False
        if _clock() >= self._retry_at:
            self._send_probe(monitor)
        return False

    def _send_probe(self, monitor: dict):
        """One screenshot request. The answer is read on a later grab()."""
        if self._inflight is not None and not self._inflight.done.is_set():
            self.lost += 1  # never answered; stop waiting for it
        self._inflight = None
        try:
            # The cached display, even if old: in Claude-started processes only
            # the first SCK call answers, so a fresh list would stall the tick.
            # A stale display makes the probe fail, and the next one refetches.
            key = _frame_key(monitor["left"], monitor["top"], monitor["width"], monitor["height"])
            if key not in self._displays:
                self._refresh(_PROBE_CONTENT_TIMEOUT)
            display = self._displays.get(key)
            if display is None:
                raise RuntimeError(f"no display at {monitor}")
            self._probe = self._inflight = _start(
                lambda h: self._start_screenshot(display, h), keep=False)
        except SCKTimeout as e:
            self._inflight = e.request
            self._probe_failed(f"got no display list ({e})")
        except Exception as e:
            self._displays = {}
            self._probe_failed(f"failed ({e})")
        else:
            logger.debug("ScreenCaptureKit probe sent")

    def _probe_failed(self, reason: str):
        self._probes_failed += 1
        if not self._worked and self._probes_failed >= _MAX_PROBES_NEVER_WORKED:
            self.off = True
            self._retry_at = None
            logger.warning(
                "ScreenCaptureKit probe %s; SCK never worked in this process, "
                "so using the screencapture tool from now on", reason)
            return
        self._level = min(self._level + 1, len(_RETRY_MINUTES) - 1)
        self._schedule(f"ScreenCaptureKit probe {reason}", logging.INFO)

    def _come_back(self, took: float):
        self._retry_at = None
        self._inflight = None
        self._back_at = _clock()
        logger.info("ScreenCaptureKit answered a probe in %.2fs; using it again", took)

    # ── SCK calls ────────────────────────────────────────────────────────

    def _display_for(self, monitor: dict, timeout: float):
        key = _frame_key(monitor["left"], monitor["top"], monitor["width"], monitor["height"])
        stale = _clock() - self._fetched_at > _CONTENT_MAX_AGE_SECONDS
        if stale or key not in self._displays:
            self._refresh(timeout)
        return self._displays.get(key)

    def _refresh(self, timeout: float):
        content = _await(
            lambda h: self._sck.SCShareableContent
            .getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
                False, True, h),
            timeout,
        )
        displays = {}
        for d in content.displays():
            f = d.frame()
            displays[_frame_key(f.origin.x, f.origin.y, f.size.width, f.size.height)] = d
        self._displays = displays
        self._fetched_at = _clock()

    def _start_screenshot(self, display, handler):
        sck = self._sck
        content_filter = sck.SCContentFilter.alloc().initWithDisplay_excludingWindows_(display, [])
        scale = content_filter.pointPixelScale()
        frame = display.frame()
        config = sck.SCStreamConfiguration.alloc().init()
        config.setWidth_(round(frame.size.width * scale))
        config.setHeight_(round(frame.size.height * scale))
        config.setPixelFormat_(_BGRA)
        config.setShowsCursor_(False)  # mss and `screencapture -x` leave it out too
        sck.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
            content_filter, config, handler)
