"""
Analysis Worker
Consumes screenshots from the capture queue, extracts text (a11y/OCR),
labels them with Gemma 4, then stores everything.

Two analysis modes (configurable via settings.analysis_mode):
  - "merged" (Accurate): Single LLM call with thinking (~76s). Gemma detects
    layout regions + produces analysis in one pass. Best for complex layouts.
  - "fast": No-thinking LLM call for analysis (~12s) + instant OCR-based
    layout clustering. 6x faster, no LLM needed for layout.

Per-app pHash cache avoids redundant processing for similar screens:
  - identical (diff <= 2): skip OCR + Gemma, reuse everything from cache
  - minor (diff 3-7): reuse layout + Gemma analysis, re-run OCR
  - full (diff > 7): run full pipeline
"""

import asyncio
import logging
import re
import sys
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import Image

from screenmind.config import settings
from screenmind.engine.analyzer import GemmaAnalyzer
from screenmind.engine.ocr import OCRExtractor
from screenmind.storage.database import Database
from screenmind.storage.models import ScreenshotEntry, ActivityRecord
from screenmind.workers.capture_worker import CaptureResult, filter_sensitive

logger = logging.getLogger("screenmind.workers.analysis_worker")

# Regex for extracting URLs from OCR/A11y text
_URL_RE = re.compile(
    r'https?://[^\s<>"\']+'   # Standard http(s) URLs
    r'|(?<![\w@./])'
    r'(?:www\.)[^\s<>"\']+'    # www. prefixed URLs
    , re.IGNORECASE
)
# Common noise URLs that aren't the "active page"
_URL_NOISE = {'http://localhost', 'http://127.0.0.1', 'https://fonts.googleapis.com',
              'https://cdn.', 'http://schemas.', 'chrome-extension://'}


# The Windows adapter leaves window chrome out of a11y text by element type,
# in any UI language. The macOS and Linux walkers do not, so there English
# chrome words still mark window chrome (see _a11y_is_content).
_A11Y_TYPE_FILTERED = sys.platform == "win32"
# Window chrome that every windowed app exposes to the accessibility tree.
_CHROME_MARKERS = [
    'minimize', 'maximize', 'restore', 'close',
    'tab bar', 'app bar', 'address and search bar',
    'has access to this site', 'no access needed',
    'memory usage', 'sleeping', 'extensions',
]
# macOS menu bar items. Lines made only of these are app chrome.
_MENU_WORDS = {
    'apple', 'file', 'edit', 'view', 'window', 'help', 'history', 'bookmarks',
    'profiles', 'tab', 'format', 'go', 'tools', 'shell', 'insert', 'chrome',
}
# Below this much real text, a11y is not worth skipping OCR for.
_A11Y_MIN_CONTENT_CHARS = 200
# A11y text kept as a header above OCR text only when it is this short.
_A11Y_MAX_PREFIX_CHARS = 500

# Below this much screen text, an unlabeled frame counts as an empty display
_EMPTY_SCREEN_MAX_TEXT = 20
# The menu bar clock, e.g. "Oct 7., Wed 13:30"
_CLOCK_RE = re.compile(r'\b\d{1,2}:\d{2}\b')


def _screen_text_len(text: Optional[str]) -> int:
    """Length of screen text without the macOS menu bar: menu-word lines and
    the clock. A display that shows only the menu bar has no content."""
    total = 0
    for line in (text or "").split('\n'):
        low = line.strip().lower()
        words = [w for w in re.split(r'[\s|/·•]+', low) if w]
        if not words or all(w in _MENU_WORDS for w in words):
            continue
        # One OCR line for the whole bar: the app name, then its menus
        if len(words) >= 3 and all(w in _MENU_WORDS for w in words[1:]):
            continue
        if len(low) <= 25 and _CLOCK_RE.search(low):
            continue
        total += len(low)
    return total


def _a11y_is_content(text: Optional[str], window_title: Optional[str] = None,
                     app_name: Optional[str] = None,
                     type_filtered: bool = _A11Y_TYPE_FILTERED) -> bool:
    """Whether a11y text is real window content, good enough to replace OCR.

    False for the window title or app name on its own, text that is mostly
    the same lines repeated, and too little text. These rules are structural
    and hold in any UI language.

    type_filtered: the adapter already left window chrome out by element
    type (Windows: buttons, toolbars, tabs; browsers give only the page).
    Elsewhere (macOS keeps buttons and tabs, Linux filters nothing) English
    chrome markers and menu-word lines are also checked.
    """
    if not text:
        return False
    if not type_filtered and sum(1 for m in _CHROME_MARKERS if m in text.lower()) >= 3:
        return False
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    if not lines:
        return False
    if len(lines) >= 10 and len(set(lines)) / len(lines) < 0.5:
        return False  # mostly repeats
    skip = {(window_title or '').strip().lower(), (app_name or '').strip().lower()}
    content = []
    for line in dict.fromkeys(lines):
        low = line.lower()
        if low in skip:
            continue
        if not type_filtered:
            words = [w for w in re.split(r'[\s|/·•]+', low) if w]
            if words and all(w in _MENU_WORDS for w in words):
                continue
        content.append(line)
    return sum(len(l) for l in content) >= _A11Y_MIN_CONTENT_CHARS


def _extract_url(text: str) -> str | None:
    """Extract the most likely active-page URL from OCR/A11y text.

    Strategy: find all URLs, filter noise, pick the first "real" one.
    Address bar text usually appears near the top of OCR output.
    """
    urls = _extract_all_urls(text)
    return urls[0] if urls else None


def _extract_all_urls(text: str) -> list[str]:
    """Extract all unique, non-noise URLs from OCR/A11y text."""
    if not text:
        return []
    matches = _URL_RE.findall(text)
    seen = set()
    result = []
    for url in matches:
        url = url.rstrip('.,;:)]\'"')  # Strip trailing punctuation
        if len(url) < 10:
            continue
        if any(url.startswith(n) for n in _URL_NOISE):
            continue
        if url.lower() not in seen:
            seen.add(url.lower())
            result.append(url)
    return result


class AnalysisWorker:
    """
    Background worker that processes queued screenshots through the full pipeline:
    1. OCR text extraction (fast, feeds into Gemma as context)
    2. Gemma 4 analysis (merged, balanced or fast mode) + Layout detection (Gemma or OCR clustering)
    3. Database storage
    """

    _APP_CACHE_MAX = 30  # Max entries in per-app LRU cache

    def __init__(self, queue: asyncio.Queue, database: Database):
        self._queue = queue
        self._db = database
        self._analyzer = GemmaAnalyzer()
        self._ocr = OCRExtractor()
        self._running = False
        self._processed = 0
        self._errors = 0
        self._is_backfill = False  # Set by _backfill_skipped for method labeling

        # Per-app analysis cache: (app_name, title) -> cached results
        # Avoids redundant Gemma calls for identical/similar screens
        self._app_cache: OrderedDict = OrderedDict()
        self._cache_hits = 0
        self._cache_skips = 0

    async def run(self):
        """Main processing loop."""
        self._running = True

        logger.info("Started. Waiting for screenshots...")
        self._last_queue_log = 0  # Track periodic queue depth logging

        # Startup scan: count unanalyzed entries from previous session
        try:
            conn = self._db._get_conn()
            pending = conn.execute(
                "SELECT COUNT(*) FROM activities WHERE status IN ('pending', 'skipped', 'failed') AND DATE(timestamp) = DATE('now', 'localtime')"
            ).fetchone()[0]
            if pending:
                logger.info(f"Found {pending} unanalyzed entries — will backfill during idle")
        except Exception:
            pass

        while self._running:
            try:
                try:
                    capture: CaptureResult = await asyncio.wait_for(
                        self._queue.get(), timeout=2.0
                    )
                except asyncio.TimeoutError:
                    # Queue empty — check for skipped entries to backfill
                    if self._queue.qsize() == 0:
                        await self._backfill_skipped()
                    # Log queue depth every 60s so user knows items are pending
                    qsize = self._queue.qsize()
                    now = time.time()
                    if qsize > 0 and now - self._last_queue_log > 60:
                        logger.info(f"Queue: {qsize} screenshots pending")
                        self._last_queue_log = now
                    continue

                # Deferred analysis: wait until idle (60s no new items)
                if settings.defer_analysis:
                    while True:
                        try:
                            next_item = await asyncio.wait_for(
                                self._queue.get(), timeout=60.0
                            )
                            # Got another item before idle timeout — keep the latest
                            capture = next_item
                        except asyncio.TimeoutError:
                            # 60s idle — time to process
                            break

                # Time-based staleness skip: don't analyze captures > 3 min old
                # (they're stale — user has moved on).
                age_seconds = (datetime.now() - capture.timestamp).total_seconds()
                if age_seconds > 180:
                    if capture.activity_id:
                        # Keep what was read at capture time; no OCR, no Gemma.
                        self._db.mark_skipped(
                            capture.activity_id,
                            ocr_text=filter_sensitive(capture.a11y_text),
                            active_url=capture.browser_url,
                        )
                    self._cache_skips += 1
                    self._queue.task_done()
                    logger.debug(f"Skipped stale capture ({age_seconds:.0f}s old)")
                    continue

                await self._process(capture)
                self._queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                self._errors += 1

    async def _process(self, capture: CaptureResult):
        """Process a single screenshot through the full pipeline."""
        start = time.time()

        # Guard: skip if screenshot was deleted (user cleared timeline mid-queue)
        if capture.filepath and not capture.filepath.exists():
            logger.debug(f"Skipping — screenshot deleted: {capture.filepath.name}")
            return

        # Guard: skip if DB row was deleted (user cleared timeline after capture)
        if capture.activity_id:
            row_exists = self._db._get_conn().execute(
                "SELECT 1 FROM activities WHERE id = ?", (capture.activity_id,)
            ).fetchone()
            if not row_exists:
                logger.debug(f"Skipping — activity #{capture.activity_id} deleted from DB")
                return

        # Use existing DB entry (inserted by CaptureWorker for instant timeline display)
        # or create one if not present (backward compat)
        if capture.activity_id:
            activity_id = capture.activity_id
        else:
            entry = ScreenshotEntry(
                timestamp=capture.timestamp,
                screenshot_path=str(capture.filepath),
                window_title=capture.window_title,
                detected_app_name=capture.app_name,
                analyzed=False,
            )
            activity_id = self._db.insert_activity(entry)

        try:
            logger.info(f"Processing #{activity_id} ({capture.app_name or 'unknown'})...")

            # 2. Per-app cache check — skip OCR + Gemma for identical screens
            #    Compare pHash against last analyzed frame for same (app, title).
            cache_key = (capture.app_name or "unknown", (capture.window_title or "")[:100])
            cached = self._app_cache.get(cache_key)
            tier = "full"  # default: full Gemma call

            if cached and capture.phash:
                phash_diff = capture.phash - cached["phash"]
                cache_age = time.time() - cached["timestamp"]

                # Communication apps change content faster — shorter stale window
                _app_lower = (capture.app_name or "").lower()
                _is_comms = any(c in _app_lower for c in ("discord", "slack", "teams", "whatsapp", "telegram", "gmail", "outlook", "mail"))
                stale_limit = 240 if _is_comms else 420  # 4min comms, 7min others

                if phash_diff <= 3:
                    tier = "identical"
                elif phash_diff <= 10 and cache_age < stale_limit:
                    tier = "minor"
                # else: 11+ or stale -> full pipeline

            # --- Tier "identical": copy everything from cache, skip OCR entirely ---
            if tier == "identical":
                active_url = capture.browser_url or cached.get("active_url")
                method_label = "backfill:cache:identical" if self._is_backfill else "cache:identical"
                self._db.update_activity_analysis(
                    activity_id=activity_id,
                    analysis=cached["analysis"],
                    ocr_text=cached.get("ocr_text"),
                    ocr_boxes=cached.get("ocr_boxes_json"),
                    organized_text=cached.get("organized_text"),
                    analysis_method=method_label,
                    active_url=active_url,
                )
                self._cache_hits += 1
                elapsed = time.time() - start
                self._processed += 1
                logger.info(f"#{self._processed} in {elapsed:.1f}s: "
                      f"{cached['analysis'].app_name} ({cached['analysis'].activity_category}) "
                      f"[cache: identical]")
                return

            # 3. Text extraction (only for minor/full tiers)
            ocr_text = None
            ocr_boxes = None
            ocr_boxes_json = None
            text_method = "none"

            # 3a. Use a11y text captured at screenshot time (correct window)
            a11y_text = capture.a11y_text

            # Detect if a11y text is just window chrome (buttons, menus, tabs,
            # the window title) vs actual app content.
            a11y_is_content = _a11y_is_content(a11y_text, capture.window_title, capture.app_name)

            if a11y_text and a11y_is_content:
                # A11y has real content (native apps like Notepad, File Explorer)
                # Skip OCR — a11y is more accurate and faster
                ocr_text = a11y_text
                text_method = "a11y"
            elif a11y_text and len(a11y_text.strip()) > 20:
                # A11y got some text but it's chrome — keep for metadata, will use OCR as primary
                text_method = "a11y"

            # 3b. OCR — runs when a11y text is chrome-only or insufficient
            needs_ocr = not a11y_is_content or text_method == "none"
            if needs_ocr and self._ocr.is_available:
                ocr_raw, ocr_boxes = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self._ocr.extract_text_with_boxes(capture.image)
                )

                if ocr_raw:
                    if a11y_is_content and ocr_text:
                        # A11y has real content — merge OCR extras (rare path)
                        a11y_lower = ocr_text.lower()
                        ocr_extras = []
                        for line in ocr_raw.split('\n'):
                            line_stripped = line.strip()
                            if line_stripped and len(line_stripped) > 3 and line_stripped.lower() not in a11y_lower:
                                ocr_extras.append(line_stripped)
                        if ocr_extras:
                            ocr_text += '\n--- (from image OCR) ---\n' + '\n'.join(ocr_extras)
                            text_method = "a11y+ocr"
                    else:
                        # A11y is chrome or empty — OCR is the primary source
                        # Prepend short a11y chrome for metadata (window title, URL)
                        if a11y_text and len(a11y_text) <= _A11Y_MAX_PREFIX_CHARS:
                            ocr_text = a11y_text + '\n--- (from image OCR) ---\n' + ocr_raw
                            text_method = "a11y+ocr"
                        else:
                            ocr_text = ocr_raw
                            text_method = "ocr"

            # 3c. Sensitive data filter — redact before AI + storage. The boxes
            #     too: they are stored and organized_text is built from them.
            ocr_text = filter_sensitive(ocr_text)
            if ocr_boxes and settings.sensitive_filter_enabled:
                try:
                    from screenmind.privacy.data_filter import filter_ocr_boxes, parse_enabled_types
                    filter_ocr_boxes(ocr_boxes, parse_enabled_types(settings.sensitive_filter_types))
                except Exception as e:
                    logger.warning(f"Sensitive filter error (OCR boxes): {e}")
            if ocr_boxes:
                import json
                ocr_boxes_json = json.dumps(ocr_boxes)

            # 3c'. Nothing on screen: no window label and almost no text (e.g. a
            #      display showing only the wallpaper). Skip Gemma. Without hints
            #      it guesses a category ("coding"), and an empty display would
            #      count as work time. Also saves a GPU call per empty display.
            if (not capture.app_name and not capture.window_title
                    and _screen_text_len(ocr_text) < _EMPTY_SCREEN_MAX_TEXT):
                self._db.update_activity_analysis(
                    activity_id=activity_id,
                    analysis=ActivityRecord(
                        activity_category="idle",
                        activity_summary="No app or text on this display",
                        mood="neutral",
                        confidence=1.0,
                    ),
                    ocr_text=ocr_text,
                    ocr_boxes=ocr_boxes_json,
                    analysis_method="rule:empty_screen",
                )
                self._processed += 1
                logger.info(f"#{self._processed} in {time.time() - start:.1f}s: "
                            f"empty display (idle) [no Gemma]")
                return

            # 3d. Extract URLs from text (for Gemma hint + DB storage)
            found_urls = _extract_all_urls(ocr_text)
            if capture.browser_url and capture.browser_url not in found_urls:
                found_urls.insert(0, capture.browser_url)
            # Only the URL the browser itself reports. A URL found in the screen
            # text is usually a link in the page or a chat, not the page (Chrome
            # hides https:// in the address bar), and a wrong URL is worse than none.
            active_url = capture.browser_url

            # --- Tier "minor": run OCR (already done above), reuse Gemma + layout ---
            if tier == "minor":
                analysis = cached["analysis"]
                layout_regions = cached["regions"]

                # Rebuild organized text with NEW OCR boxes + CACHED layout regions
                organized_text = None
                if layout_regions and ocr_boxes:
                    try:
                        from screenmind.engine.layout_analyzer import organize_ocr_text
                        screen_w, screen_h = capture.image.size
                        organized_text = organize_ocr_text(ocr_boxes, layout_regions, screen_w, screen_h)
                        if organized_text:
                            text_method += "+layout"
                    except Exception as e:
                        logger.debug(f"Text organization failed (non-fatal): {e}")

                self._cache_hits += 1
                tier_label = "cache: minor"
                logger.info(f"Processing #{activity_id} [{tier_label}] ...")
                # Falls through to: DB update
            else:
                # --- Tier "full": run Gemma analysis + layout detection ---
                tier_label = "full"

                async def _run_analysis():
                    """Gemma 4 analysis + layout with smart OOM retry."""
                    OOM_KEYWORDS = {"memory", "oom", "resource", "allocat", "failed to load"}
                    _MODE_MAP = {
                        "fast": self._analyzer.analyze_screenshot_fast,
                        "balanced": self._analyzer.analyze_screenshot_balanced,
                        "merged": self._analyzer.analyze_screenshot,
                    }
                    analyze_fn = _MODE_MAP.get(settings.analysis_mode, self._analyzer.analyze_screenshot_fast)
                    max_retries = 3
                    for attempt in range(max_retries):
                        try:
                            return await asyncio.get_event_loop().run_in_executor(
                                None,
                                lambda: analyze_fn(
                                    image=capture.image,
                                    window_title=capture.window_title,
                                    app_name_hint=capture.app_name,
                                    ocr_text=ocr_text,
                                    active_urls=found_urls,
                                    user_actions=capture.user_actions,
                                ),
                            )
                        except Exception as e:
                            err_msg = str(e).lower()
                            is_oom = any(kw in err_msg for kw in OOM_KEYWORDS)
                            if attempt < max_retries - 1:
                                wait = 15 * (attempt + 1) if is_oom else 2 ** (attempt + 1)
                                kind = "OOM" if is_oom else "Error"
                                logger.warning(f"{kind} (attempt {attempt + 1}), retry in {wait}s: {e}", exc_info=is_oom)
                                await asyncio.sleep(wait)
                            else:
                                retry_count = getattr(capture, '_retry_count', 0)
                                if retry_count < 2 and is_oom:
                                    capture._retry_count = retry_count + 1
                                    await self._queue.put(capture)
                                    logger.warning(f"Re-queued (attempt {retry_count + 1}/2): {e}", exc_info=True)
                                    return None
                                raise

                result = await _run_analysis()

                if result is None:
                    return  # Re-queued or fatal

                analysis, layout_regions = result

                # ── Quality gate: retry once if critical fields are missing ──
                _missing = []
                if not analysis.activity_summary:
                    _missing.append("summary")
                if not analysis.scene_description:
                    _missing.append("scene_description")
                if not analysis.activity_category or analysis.activity_category == "other":
                    _missing.append("category")

                if _missing and not getattr(capture, '_quality_retried', False):
                    capture._quality_retried = True
                    logger.debug(f"Missing fields ({', '.join(_missing)}) — retrying analysis...")
                    await asyncio.sleep(1)
                    retry_result = await _run_analysis()
                    if retry_result:
                        retry_analysis, retry_regions = retry_result
                        # Take retry if it filled more fields
                        retry_missing = []
                        if not retry_analysis.activity_summary:
                            retry_missing.append("summary")
                        if not retry_analysis.scene_description:
                            retry_missing.append("scene_description")
                        if len(retry_missing) < len(_missing):
                            analysis, layout_regions = retry_analysis, retry_regions
                            logger.debug(f"Retry filled: {set(_missing) - set(retry_missing)}")
                        else:
                            logger.debug(f"Retry didn't improve — keeping original")

                # Organize text using layout regions + OCR boxes (geometry only, no LLM)
                organized_text = None
                if ocr_boxes:
                    try:
                        from screenmind.engine.layout_analyzer import organize_ocr_text, cluster_ocr_layout
                        screen_w, screen_h = capture.image.size
                        # Use Gemma regions if available, otherwise OCR clustering
                        regions = layout_regions if layout_regions else cluster_ocr_layout(ocr_boxes, screen_w, screen_h)
                        organized_text = organize_ocr_text(ocr_boxes, regions, screen_w, screen_h)
                        if organized_text:
                            logger.debug(f"Organized text: {len(organized_text)} chars ({len(regions)} regions)")
                            text_method += "+layout"
                    except Exception as e:
                        logger.debug(f"Text organization failed (non-fatal): {e}")

            # 4. Update DB with all results
            analysis_label = f"cache:minor" if tier == "minor" else f"full:{settings.analysis_mode}"
            if self._is_backfill:
                analysis_label = f"backfill:{analysis_label}"
            self._db.update_activity_analysis(
                activity_id=activity_id,
                analysis=analysis,
                ocr_text=ocr_text,
                ocr_boxes=ocr_boxes_json,
                organized_text=organized_text,
                analysis_method=analysis_label,
                active_url=active_url,
            )

            # 5. Update per-app cache (for both "full" and "minor" tiers)
            if capture.phash:
                self._app_cache[cache_key] = {
                    "phash": capture.phash,
                    "analysis": analysis,
                    "regions": layout_regions if tier == "full" else cached.get("regions", []),
                    "ocr_text": ocr_text,
                    "ocr_boxes_json": ocr_boxes_json,
                    "organized_text": organized_text,
                    "active_url": active_url,
                    "timestamp": cached["timestamp"] if tier == "minor" else time.time(),
                }
                # LRU eviction
                if len(self._app_cache) > self._APP_CACHE_MAX:
                    self._app_cache.popitem(last=False)

            elapsed = time.time() - start
            self._processed += 1

            # Log with context
            text_len = len(ocr_text) if ocr_text else 0
            parts = [
                f"#{self._processed} in {elapsed:.1f}s:",
                f"{analysis.app_name} ({analysis.activity_category})",
                f"-- {analysis.activity_summary[:50]}",
                f"[text: {text_len} chars via {text_method}]",
                f"[{tier_label}]",
            ]
            if capture.user_actions:
                parts.append(f"[actions: {capture.user_actions.count(chr(10)) + 1}]")

            logger.info(" ".join(parts))

        except Exception as e:
            elapsed = time.time() - start
            self._errors += 1
            logger.error(f"Failed after {elapsed:.1f}s: {e}")

            self._db.update_activity_analysis(
                activity_id=activity_id,
                analysis=ActivityRecord(
                    app_name=capture.app_name or "unknown",
                    activity_category="other",
                    activity_summary=f"Analysis failed: {str(e)[:100]}",
                    confidence=0.0,
                ),
                status="failed",
            )

    async def _backfill_skipped(self):
        """Re-analyze one unanalyzed or skipped entry when idle.
        Catches: entries from crashes (analyzed=0) + stale skips.
        """
        try:
            conn = self._db._get_conn()
            row = conn.execute(
                """SELECT id, screenshot_path, window_title, COALESCE(detected_app, app_name), ocr_text, ocr_boxes,
                          user_actions, status, active_url
                   FROM activities
                   WHERE status IN ('pending', 'skipped', 'failed')
                     AND DATE(timestamp) = DATE('now', 'localtime')
                   ORDER BY timestamp DESC LIMIT 1""",
            ).fetchone()

            if not row:
                return  # No skipped entries — nothing to backfill

            (activity_id, ss_path, window_title, app_name, ocr_text, ocr_boxes_raw,
             user_actions, status, active_url) = row

            # Check screenshot still exists on disk
            if not ss_path or not Path(ss_path).exists():
                # Screenshot deleted — mark as permanently unprocessable
                conn.execute(
                    "UPDATE activities SET analyzed = 1, status = 'dead', summary = 'Skipped (screenshot deleted)' WHERE id = ?",
                    (activity_id,),
                )
                conn.commit()
                return

            # Abort if fresh captures arrived
            if self._queue.qsize() > 0:
                return

            logger.info(f"Backfilling #{activity_id} ({app_name})...")

            # Load image and create a minimal CaptureResult
            try:
                from screenmind.privacy.encryption import open_image
                img = open_image(ss_path)
                img.load()  # Force full decode — catches truncated files
            except Exception as img_err:
                # Corrupt/truncated screenshot — mark as permanently failed
                logger.warning(f"Backfill #{activity_id}: corrupt image, skipping permanently ({img_err})")
                conn.execute(
                    "UPDATE activities SET analyzed = 1, status = 'dead', summary = 'Skipped (corrupt screenshot)' WHERE id = ?",
                    (activity_id,),
                )
                conn.commit()
                return

            import imagehash
            phash = imagehash.phash(img)

            capture = CaptureResult(
                filepath=Path(ss_path),
                timestamp=datetime.now(),  # Use now — it's no longer stale
                window_title=window_title,
                app_name=app_name,
                image=img,
                activity_id=activity_id,
                # A skipped frame kept its capture-time a11y text and URL
                a11y_text=ocr_text if status == 'skipped' else None,
                phash=phash,
                user_actions=user_actions,
                browser_url=active_url,
            )

            self._is_backfill = True
            try:
                await self._process(capture)
            finally:
                self._is_backfill = False
            logger.info(f"Backfill #{activity_id} complete")

        except Exception as e:
            logger.error(f"Backfill error: {e}")

    def stop(self):
        self._running = False
        logger.info(
            f"Stopped. "
            f"Processed: {self._processed}, Errors: {self._errors}"
        )

    def flush_queue(self):
        """Drain all pending items from the analysis queue.
        Called when user clears timeline to prevent stale items
        from blocking fresh captures.
        """
        flushed = 0
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                flushed += 1
            except Exception:
                break
        if flushed:
            logger.debug(f"Flushed {flushed} queued items")

    @property
    def stats(self) -> dict:
        return {
            "running": self._running,
            "processed": self._processed,
            "errors": self._errors,
            "queue_size": self._queue.qsize(),
            "cache_hits": self._cache_hits,
            "cache_skips": self._cache_skips,
            "cache_size": len(self._app_cache),
        }
