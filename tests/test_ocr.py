"""Test OCR extraction functionality."""
import pytest
from PIL import Image


def test_ocr_extractor_init():
    """OCRExtractor initializes without crashing."""
    from screenmind.engine.ocr import OCRExtractor
    ocr = OCRExtractor()
    assert ocr is not None


def test_ocr_extract_text_returns_string():
    """extract_text returns a string (or None) for a blank image."""
    from screenmind.engine.ocr import OCRExtractor
    ocr = OCRExtractor()
    if not ocr.is_available:
        pytest.skip("RapidOCR not available")
    img = Image.new("RGB", (200, 100), color="white")
    result = ocr.extract_text(img)
    assert result is None or isinstance(result, str)


def test_ocr_extract_text_with_boxes_format():
    """extract_text_with_boxes returns (text, boxes) tuple."""
    from screenmind.engine.ocr import OCRExtractor
    ocr = OCRExtractor()
    if not ocr.is_available:
        pytest.skip("RapidOCR not available")
    img = Image.new("RGB", (200, 100), color="white")
    text, boxes = ocr.extract_text_with_boxes(img)
    assert text is None or isinstance(text, str)
    assert boxes is None or isinstance(boxes, list)
