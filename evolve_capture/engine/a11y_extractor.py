"""
Accessibility API Text Extractor
Extracts structured text from the active window using OS accessibility APIs.
This is MORE accurate than OCR because it reads actual UI element text directly.

Falls back gracefully if the window doesn't support accessibility (games, remote desktop).

This module now delegates to the platform_support adapter for cross-platform
compatibility. The API (A11yExtractor class) remains identical.
"""

import logging
from typing import Optional, Tuple

from evolve_capture.platform_support import adapter as get_adapter

logger = logging.getLogger("evolve_capture.engine.a11y_extractor")


class A11yExtractor:
    """
    Extracts text from the foreground window using OS accessibility APIs.

    Advantages over OCR:
    - 100% accurate text (reads actual rendered text, no misreads)
    - Instant (<0.5s vs 8-15s for OCR)
    - Structured (knows what's a button vs a label vs a message)
    - Zero GPU/CPU cost

    Limitations:
    - Doesn't work on: games, remote desktop, some Electron apps, images
    - Only captures the ACTIVE window (not the full screen)
    """

    def __init__(self):
        self._adapter = get_adapter()

    def extract_text(self, hwnd: Optional[int] = None) -> Tuple[Optional[str], str]:
        """
        Extract all visible text from the foreground window.

        Args:
            hwnd: Optional window handle. If None, uses the foreground window.

        Returns:
            Tuple of (text_content, extraction_method)
            - text_content: All text from the window, structured by UI elements
            - extraction_method: "a11y" if successful, "none" if failed
        """
        try:
            return self._adapter.extract_a11y_text(hwnd)
        except Exception as e:
            logger.debug(f"Extraction failed: {e}")
            return None, "none"

    @property
    def is_available(self) -> bool:
        return self._adapter.is_a11y_available()
