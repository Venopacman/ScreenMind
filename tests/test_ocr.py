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
        pytest.skip("EasyOCR not available")
    img = Image.new("RGB", (200, 100), color="white")
    result = ocr.extract_text(img)
    assert result is None or isinstance(result, str)


def test_ocr_extract_text_with_boxes_format():
    """extract_text_with_boxes returns (text, boxes) tuple."""
    from screenmind.engine.ocr import OCRExtractor
    ocr = OCRExtractor()
    if not ocr.is_available:
        pytest.skip("EasyOCR not available")
    img = Image.new("RGB", (200, 100), color="white")
    text, boxes = ocr.extract_text_with_boxes(img)
    assert text is None or isinstance(text, str)
    assert boxes is None or isinstance(boxes, list)


def test_ocr_passes_canvas_size_to_detector():
    """readtext gets the reduced detector canvas (memory bound)."""
    from unittest.mock import MagicMock
    from screenmind.engine.ocr import OCRExtractor, OCR_CANVAS_SIZE
    ocr = OCRExtractor()
    ocr._reader = MagicMock()
    ocr._reader.readtext.return_value = [([[0, 0], [10, 0], [10, 10], [0, 10]], "hello", 0.9)]
    text, boxes = ocr.extract_text_with_boxes(Image.new("RGB", (200, 100), "white"))
    assert ocr._reader.readtext.call_args.kwargs["canvas_size"] == OCR_CANVAS_SIZE
    assert text == "hello"


def test_use_gpu_only_for_cuda(monkeypatch):
    """Apple MPS is skipped; GPU is used only when CUDA is available."""
    torch = pytest.importorskip("torch")
    from screenmind.engine import ocr
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    assert ocr._use_gpu() is False
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    assert ocr._use_gpu() is True
