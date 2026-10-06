"""
OCR Engine — Text Extraction from Screenshots
Extracts visible text to enhance Gemma 4's analysis context.
Uses easyocr with dark-theme preprocessing for robust recognition.
"""

import logging
import time
from typing import Optional

from PIL import Image, ImageOps, ImageEnhance

from screenmind.config import settings

logger = logging.getLogger("screenmind.engine.ocr")

# Longest side of the image fed to the CRAFT text detector (EasyOCR default 2560).
# Detector memory grows with this; recognition still reads crops from the
# full-resolution image, so most text survives a smaller value.
# Measured on 3024x1964 screenshots, CPU: 2560 ~7-8 GB peak, 1280 ~6 GB,
# 960 ~4 GB (96% of text), 800 ~3.1 GB (90%), 640 ~2.1 GB (71%).
OCR_CANVAS_SIZE = 800


def _use_gpu() -> bool:
    """Use CUDA when present. Skip Apple MPS: PyTorch's MPS allocator kept
    ~7.5 GB of unified memory between frames, and CPU is fast enough here."""
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:
        return False


# EasyOCR allows a Cyrillic model only with English, so Cyrillic codes get a
# second Reader that recognizes the same boxes (detector=False).
CYRILLIC_LANGS = {"ru", "rs_cyrillic", "be", "bg", "uk", "mn", "abq", "ady",
                  "kbd", "ava", "dar", "inh", "che", "lbe", "lez", "tab", "tjk"}

# Take the Cyrillic reading when it is at most this much less confident than Latin
CYRILLIC_CONF_MARGIN = 0.05

# Cyrillic letters that look like Latin ones. The Cyrillic model reads "MCP"
# as "MCР" (Cyrillic Р) with high confidence; these are not proof of Russian.
_CYR_TO_LAT = str.maketrans("АВЕКМНОРСТХаеорсух", "ABEKMHOPCTXaeopcyx")
_LAT_TO_CYR = str.maketrans("ABEKMHOPCTXaeopcyx", "АВЕКМНОРСТХаеорсух")
_CYR_LOOKALIKES = set("АВЕКМНОРСТХаеорсух")


def _split_languages(langs: list) -> tuple:
    """Split codes into (primary, cyrillic). Primary always includes 'en'."""
    primary = [l for l in langs if l not in CYRILLIC_LANGS]
    cyrillic = [l for l in langs if l in CYRILLIC_LANGS]
    if "en" not in primary:
        primary.insert(0, "en")
    if cyrillic and "en" not in cyrillic:
        cyrillic.append("en")
    return primary, cyrillic


def _is_cyrillic(ch: str) -> bool:
    return "\u0400" <= ch <= "\u04ff"


def _has_cyrillic_only_letters(text: str) -> bool:
    """True if text has a Cyrillic letter with no Latin twin (б, д, ж, и, л, п, я, ...)."""
    return any(_is_cyrillic(ch) and ch not in _CYR_LOOKALIKES for ch in text)


def _fix_lookalikes(text: str) -> str:
    """Make each mixed-script word a single script, by majority of its letters.

    "MCР" (Cyrillic Р) -> "MCP"; "Пpивет" (Latin p) -> "Привет".
    """
    words = text.split(" ")
    for i, word in enumerate(words):
        cyr = sum(1 for ch in word if _is_cyrillic(ch))
        lat = sum(1 for ch in word if ch.isascii() and ch.isalpha())
        if cyr and lat:
            words[i] = word.translate(_CYR_TO_LAT) if lat >= cyr else word.translate(_LAT_TO_CYR)
    return " ".join(words)


def _merge_readings(latin: list, cyrillic: list) -> list:
    """Pick the Latin or Cyrillic reading per box.

    Both lists come from recognize() over the same boxes, one result per box,
    in the same order. Cyrillic wins only if it holds a Cyrillic-only letter
    and is about as confident; this keeps English text away from lookalikes.
    """
    if len(latin) != len(cyrillic):
        logger.debug(f"Latin/Cyrillic result count differs ({len(latin)} vs {len(cyrillic)}), using Latin")
        cyrillic = [None] * len(latin)
    merged = []
    for lat, cyr in zip(latin, cyrillic):
        bbox, text, conf = lat
        if cyr and _has_cyrillic_only_letters(cyr[1]) and cyr[2] >= conf - CYRILLIC_CONF_MARGIN:
            text, conf = cyr[1], cyr[2]
        merged.append((bbox, _fix_lookalikes(text), conf))
    return merged


class OCRExtractor:
    """
    Lightweight OCR that extracts screen text to feed into Gemma 4
    as additional context, improving analysis accuracy.
    """

    def __init__(self):
        self._reader = None      # detector + Latin-script recognizer
        self._reader_cyr = None  # Cyrillic recognizer, only if a Cyrillic code is configured
        self._available = True

    def _ensure_reader(self):
        """Lazy-load easyocr (downloads ~100MB model on first use)."""
        if self._reader is None and self._available:
            try:
                import easyocr
            except Exception as e:
                logger.warning(f"EasyOCR unavailable, skipping text extraction: {e}")
                self._available = False
                return

            primary, cyrillic = _split_languages(settings.ocr_languages_list)
            gpu = _use_gpu()
            try:
                self._reader = easyocr.Reader(primary, gpu=gpu, verbose=False)
            except Exception as e:
                # e.g. an unsupported mix like ch_sim + de; keep OCR working in English
                logger.warning(f"OCR languages {primary} rejected ({e}); falling back to English")
                primary = ["en"]
                try:
                    self._reader = easyocr.Reader(primary, gpu=gpu, verbose=False)
                except Exception as e2:
                    logger.warning(f"EasyOCR unavailable, skipping text extraction: {e2}")
                    self._available = False
                    return

            if cyrillic:
                try:
                    self._reader_cyr = easyocr.Reader(cyrillic, gpu=gpu, verbose=False, detector=False)
                except Exception as e:
                    logger.warning(f"Cyrillic OCR {cyrillic} unavailable ({e}); continuing without it")
                    cyrillic = []

            logger.info(f"EasyOCR initialized (languages: {', '.join(sorted(set(primary + cyrillic)))})")

    def _preprocess(self, image: Image.Image) -> Image.Image:
        """
        Preprocess screenshot for maximum OCR accuracy.
        
        Optimized via A/B testing (5 rounds, 7 strategies):
        - Ensure minimum 1920px width (upscale if needed)
        - Grayscale + sharpen + contrast 1.5
        - Only invert for very dark screens (<100 brightness)
        - Result: eCAS detection 0.04 → 0.54 confidence
        """
        import numpy as np
        from PIL import ImageFilter

        # Ensure minimum 1920px width for reliable text detection
        min_width = 1920
        if image.size[0] < min_width:
            scale = min_width / image.size[0]
            image = image.resize(
                (int(image.size[0] * scale), int(image.size[1] * scale)),
                Image.Resampling.LANCZOS,
            )

        # Convert to grayscale
        gray = image.convert("L")

        # Check brightness for dark theme handling
        avg_brightness = np.mean(np.array(gray))

        if avg_brightness < 100:
            # Very dark theme — invert first
            gray = ImageOps.invert(gray)

        # Sharpen text edges
        gray = gray.filter(ImageFilter.SHARPEN)

        # Mild contrast boost (1.5 is the sweet spot — 2.2 destroyed text)
        result = ImageEnhance.Contrast(gray).enhance(1.5)

        return result

    def extract_text(self, image: Image.Image) -> Optional[str]:
        """Extract text only (backward compatible)."""
        text, _ = self.extract_text_with_boxes(image)
        return text

    def extract_text_with_boxes(self, image: Image.Image):
        """
        Extract text AND bounding boxes from a screenshot.

        Returns:
            Tuple of (text_string, boxes_list) where boxes_list is a list of
            {"box": [[x1,y1],[x2,y2],[x3,y3],[x4,y4]], "text": str, "conf": float}
            Coordinates are relative to the ORIGINAL image size.
        """
        if not self._available:
            return None, []

        self._ensure_reader()
        if self._reader is None:
            return None, []

        try:
            import numpy as np
            start = time.time()

            # Track scale for coordinate mapping back to original
            orig_w, orig_h = image.size

            # Preprocess: resize, dark-theme inversion, contrast boost
            processed = self._preprocess(image)
            proc_w, proc_h = processed.size

            # Scale factors to map preprocessed coords back to original image
            scale_x = orig_w / proc_w
            scale_y = orig_h / proc_h

            img_array = np.array(processed)

            # Run OCR with word-level detail (paragraph=False for per-word boxes)
            if self._reader_cyr is None:
                results = self._reader.readtext(
                    img_array, detail=1, paragraph=False,
                    batch_size=4,
                    canvas_size=OCR_CANVAS_SIZE,
                )
            else:
                results = self._read_with_cyrillic(img_array)

            texts = []
            boxes = []
            for result in results:
                if len(result) == 3:
                    bbox, text, conf = result
                elif len(result) == 2:
                    bbox, text = result
                    conf = 0.5
                else:
                    continue

                text = text.strip()
                if conf > 0.2 and len(text) > 1:
                    texts.append(text)
                    # Scale bbox coords back to original image coordinates
                    scaled_box = [[int(pt[0] * scale_x), int(pt[1] * scale_y)] for pt in bbox]
                    boxes.append({"box": scaled_box, "text": text, "conf": round(conf, 2)})

            elapsed = time.time() - start
            full_text = "\n".join(texts)

            if texts:
                logger.debug(f"Extracted {len(texts)} text blocks in {elapsed:.1f}s")

            return (full_text if full_text else None), boxes

        except Exception as e:
            logger.error(f"Extraction failed: {e}")
            return None, []

    def _read_with_cyrillic(self, img_array):
        """Detect boxes once, recognize with both models, keep the better reading per box."""
        from easyocr.utils import reformat_input

        img, img_grey = reformat_input(img_array)
        horizontal, free = self._reader.detect(img, canvas_size=OCR_CANVAS_SIZE, reformat=False)
        horizontal, free = horizontal[0], free[0]
        if not horizontal and not free:
            return []
        kwargs = dict(detail=1, paragraph=False, batch_size=4, reformat=False)
        latin = self._reader.recognize(img_grey, horizontal, free, **kwargs)
        cyrillic = self._reader_cyr.recognize(img_grey, horizontal, free, **kwargs)
        return _merge_readings(latin, cyrillic)

    @property
    def is_available(self) -> bool:
        return self._available
