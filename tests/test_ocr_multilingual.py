"""Tests for OCR language handling and result parsing (RapidOCR is faked)."""
import sys
import types
from enum import Enum

import numpy as np
import pytest
from PIL import Image

from screenmind.engine import ocr
from screenmind.engine.ocr import OCRExtractor, _fix_lookalikes, _rec_model

BOX = [[0.0, 0.0], [10.5, 0.0], [10.5, 10.0], [0.0, 10.0]]


class TestRecModel:
    @pytest.mark.parametrize("langs, model", [
        (["en"], "en"),
        ([], "en"),
        (["en", "ru"], "eslav"),
        (["uk"], "eslav"),
        (["en", "bg"], "cyrillic"),
        (["en", "es", "de", "fr"], "latin"),
        (["ja"], "ch"),
        (["en", "ko"], "korean"),
        (["en", "hi"], "devanagari"),
    ])
    def test_maps_languages_to_one_recognizer(self, langs, model):
        assert _rec_model(langs) == model

    def test_mixed_scripts_use_the_first(self):
        assert _rec_model(["en", "ru", "es"]) == "eslav"
        assert _rec_model(["en", "es", "ru"]) == "latin"


class TestLookalikes:
    @pytest.mark.parametrize("raw, fixed", [
        ("MCР", "MCP"),            # Cyrillic Р in an English word
        ("Пpивет", "Привет"),      # Latin p in a Russian word
        ("hello мир", "hello мир"),
    ])
    def test_fix_lookalikes(self, raw, fixed):
        assert _fix_lookalikes(raw) == fixed


@pytest.fixture
def fake_rapidocr(monkeypatch):
    """Fake rapidocr module: records the params of each engine built."""
    mod = types.ModuleType("rapidocr")
    mod.built = []
    mod.LangRec = Enum("LangRec", {v.upper(): v for v in
                                   ["en", "latin", "eslav", "cyrillic", "ch", "korean"]})
    mod.ModelType = Enum("ModelType", {"TINY": "tiny", "MOBILE": "mobile"})
    mod.OCRVersion = Enum("OCRVersion", {"PPOCRV5": "PP-OCRv5", "PPOCRV6": "PP-OCRv6"})
    mod.result = types.SimpleNamespace(boxes=None, txts=None, scores=None)

    class RapidOCR:
        def __init__(self, params):
            self.params = params
            mod.built.append(self)

        def __call__(self, image):
            self.image = image
            return mod.result

    mod.RapidOCR = RapidOCR
    monkeypatch.setitem(sys.modules, "rapidocr", mod)
    monkeypatch.setattr(ocr, "_ocr_models_dir", lambda: "/tmp/ocr-models")
    return mod


def _with_langs(monkeypatch, value):
    monkeypatch.setattr(ocr.settings, "ocr_languages", value)


def test_engine_uses_v6_detector_and_language_recognizer(fake_rapidocr, monkeypatch):
    _with_langs(monkeypatch, "en,ru")
    o = OCRExtractor()
    o._ensure_reader()
    p = fake_rapidocr.built[0].params
    assert p["Det.ocr_version"].value == "PP-OCRv6"
    assert p["Det.model_type"].value == "tiny"
    assert p["Rec.ocr_version"].value == "PP-OCRv5"
    assert p["Rec.lang_type"].value == "eslav"
    assert p["Global.model_root_dir"] == "/tmp/ocr-models"


def test_engine_failure_disables_ocr(fake_rapidocr, monkeypatch):
    _with_langs(monkeypatch, "en")

    def boom(params):
        raise RuntimeError("model download failed")

    monkeypatch.setattr(fake_rapidocr, "RapidOCR", boom)
    o = OCRExtractor()
    assert o.extract_text_with_boxes(Image.new("RGB", (20, 20))) == (None, [])
    assert not o.is_available


def test_missing_rapidocr_disables_ocr(monkeypatch):
    monkeypatch.setitem(sys.modules, "rapidocr", None)  # import raises ImportError
    o = OCRExtractor()
    assert o.extract_text(Image.new("RGB", (20, 20))) is None
    assert not o.is_available


def test_extract_filters_and_formats(fake_rapidocr, monkeypatch):
    _with_langs(monkeypatch, "en")
    fake_rapidocr.result = types.SimpleNamespace(
        boxes=np.array([BOX, BOX, BOX]),
        txts=("hello world", "x", "noise"),
        scores=(0.95, 0.99, 0.3),
    )
    o = OCRExtractor()
    text, boxes = o.extract_text_with_boxes(Image.new("RGBA", (200, 100), "white"))
    assert text == "hello world"
    assert boxes == [{"box": [[0, 0], [10, 0], [10, 10], [0, 10]], "text": "hello world", "conf": 0.95}]
    assert fake_rapidocr.built[0].image.mode == "RGB"


def test_lookalikes_fixed_only_for_cyrillic_models(fake_rapidocr, monkeypatch):
    fake_rapidocr.result = types.SimpleNamespace(boxes=np.array([BOX]), txts=("MCР",), scores=(0.9,))
    _with_langs(monkeypatch, "en,ru")
    assert OCRExtractor().extract_text(Image.new("RGB", (20, 20))) == "MCP"
    _with_langs(monkeypatch, "en")
    assert OCRExtractor().extract_text(Image.new("RGB", (20, 20))) == "MCР"


def test_no_text_returns_none(fake_rapidocr, monkeypatch):
    _with_langs(monkeypatch, "en")
    assert OCRExtractor().extract_text_with_boxes(Image.new("RGB", (20, 20))) == (None, [])
