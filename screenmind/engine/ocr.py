"""
OCR Engine — Text Extraction from Screenshots
Extracts visible text to enhance Gemma 4's analysis context.
Uses RapidOCR (PaddleOCR models on ONNX Runtime). CPU only, no PyTorch.
"""

import logging
import time
from typing import Optional

from PIL import Image

from screenmind.config import settings

logger = logging.getLogger("screenmind.engine.ocr")

# Recognition model per language code (EasyOCR-style codes, as in OCR_LANGUAGES).
# One recognizer runs per frame, chosen by the first non-English code.
# All of these also read English.
_REC_MODELS = {
    # East Slavic: Russian, Ukrainian, Belarusian
    "ru": "eslav", "uk": "eslav", "be": "eslav",
    # Other Cyrillic scripts
    "bg": "cyrillic", "rs_cyrillic": "cyrillic", "mn": "cyrillic", "abq": "cyrillic",
    "ady": "cyrillic", "kbd": "cyrillic", "ava": "cyrillic", "dar": "cyrillic",
    "inh": "cyrillic", "che": "cyrillic", "lbe": "cyrillic", "lez": "cyrillic",
    "tab": "cyrillic", "tjk": "cyrillic",
    # PP-OCRv5's Chinese model also reads Traditional Chinese and Japanese
    "ch_sim": "ch", "ch_tra": "ch", "ja": "ch",
    "ko": "korean",
    "th": "th",
    "el": "el",
    "ar": "arabic", "fa": "arabic", "ur": "arabic", "ug": "arabic",
    "hi": "devanagari", "mr": "devanagari", "ne": "devanagari", "bh": "devanagari",
    "mai": "devanagari", "ang": "devanagari", "bho": "devanagari", "mah": "devanagari",
    "sck": "devanagari", "new": "devanagari", "gom": "devanagari", "sa": "devanagari",
    "ta": "ta",
    "te": "te",
}
_CYRILLIC_MODELS = {"eslav", "cyrillic"}

# Drop readings below this confidence (RapidOCR's own default).
_MIN_SCORE = 0.5

# Cyrillic letters that look like Latin ones. A Cyrillic model can read "MCP"
# as "MCР" (Cyrillic Р); these are not proof of Russian.
_CYR_TO_LAT = str.maketrans("АВЕКМНОРСТХаеорсух", "ABEKMHOPCTXaeopcyx")
_LAT_TO_CYR = str.maketrans("ABEKMHOPCTXaeopcyx", "АВЕКМНОРСТХаеорсух")


def _rec_model(langs: list) -> str:
    """Pick the recognition model for the configured languages.

    English-only gets the English model; any other Latin-script code gets the
    Latin model. If codes need different scripts, the first one wins.
    """
    others = [l for l in langs if l != "en"]
    if not others:
        return "en"
    models = [_REC_MODELS.get(l, "latin") for l in others]
    if len(set(models)) > 1:
        logger.warning(f"OCR reads one script at a time; using '{models[0]}' for {others[0]} "
                       f"(also configured: {', '.join(others[1:])})")
    return models[0]


def _is_cyrillic(ch: str) -> bool:
    return "Ѐ" <= ch <= "ӿ"


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


def _ocr_models_dir():
    from screenmind.engine.model_manager import _models_dir
    d = _models_dir() / "ocr"
    d.mkdir(parents=True, exist_ok=True)
    return d


class OCRExtractor:
    """
    Lightweight OCR that extracts screen text to feed into Gemma 4
    as additional context, improving analysis accuracy.

    Text detection uses PP-OCRv6 tiny (best on dark UIs and fastest in our
    tests); recognition uses the PP-OCRv5 model for the configured language.
    Models (~15 MB) download to ~/.screenmind/models/ocr on first use.
    """

    def __init__(self):
        self._engine = None
        self._rec_model = None
        self._available = True

    def _ensure_reader(self):
        """Lazy-load RapidOCR and its models."""
        if self._engine is not None or not self._available:
            return
        try:
            from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR
        except Exception as e:
            logger.warning(f"RapidOCR unavailable, skipping text extraction: {e}")
            self._available = False
            return

        rec = _rec_model(settings.ocr_languages_list)
        try:
            self._engine = RapidOCR(params={
                "Global.log_level": "warning",
                "Global.model_root_dir": str(_ocr_models_dir()),
                "Det.ocr_version": OCRVersion.PPOCRV6,
                "Det.model_type": ModelType.TINY,
                "Rec.ocr_version": OCRVersion.PPOCRV5,
                "Rec.model_type": ModelType.MOBILE,
                "Rec.lang_type": LangRec(rec),
            })
        except Exception as e:
            logger.warning(f"OCR model '{rec}' unavailable ({e}); skipping text extraction")
            self._available = False
            return
        self._rec_model = rec
        logger.info(f"OCR initialized (RapidOCR, recognizer: {rec}, "
                    f"languages: {', '.join(settings.ocr_languages_list)})")

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
            Coordinates are relative to the original image size.
        """
        if not self._available:
            return None, []

        self._ensure_reader()
        if self._engine is None:
            return None, []

        try:
            start = time.time()
            result = self._engine(image.convert("RGB"))
            txts = result.txts or ()
            scores = result.scores or ()
            raw_boxes = result.boxes if result.boxes is not None else ()

            texts = []
            boxes = []
            for bbox, text, conf in zip(raw_boxes, txts, scores):
                text = text.strip()
                if conf < _MIN_SCORE or len(text) <= 1:
                    continue
                if self._rec_model in _CYRILLIC_MODELS:
                    text = _fix_lookalikes(text)
                texts.append(text)
                boxes.append({
                    "box": [[int(pt[0]), int(pt[1])] for pt in bbox],
                    "text": text,
                    "conf": round(float(conf), 2),
                })

            if texts:
                logger.debug(f"Extracted {len(texts)} text blocks in {time.time() - start:.1f}s")
            full_text = "\n".join(texts)
            return (full_text if full_text else None), boxes

        except Exception as e:
            logger.error(f"Extraction failed: {e}")
            return None, []

    @property
    def is_available(self) -> bool:
        return self._available
