"""Tests for multi-language OCR (Latin + Cyrillic recognizers over shared boxes)."""
import sys
import types
from unittest.mock import MagicMock

import pytest
from PIL import Image

from screenmind.engine import ocr
from screenmind.engine.ocr import (
    OCRExtractor,
    _fix_lookalikes,
    _has_cyrillic_only_letters,
    _merge_readings,
    _split_languages,
)

BOX = [[0, 0], [10, 0], [10, 10], [0, 10]]


class TestSplitLanguages:
    def test_english_only(self):
        assert _split_languages(["en"]) == (["en"], [])

    def test_latin_and_cyrillic(self):
        assert _split_languages(["en", "es", "de", "fr", "ru"]) == (["en", "es", "de", "fr"], ["ru", "en"])

    def test_english_added_when_missing(self):
        assert _split_languages(["ru"]) == (["en"], ["ru", "en"])
        assert _split_languages(["de"]) == (["en", "de"], [])


class TestLookalikes:
    @pytest.mark.parametrize("raw,fixed", [
        ("MCР", "MCP"),                  # Cyrillic Р in an English word
        ("Typе KВ", "Type KB"),
        ("Пpивет мир", "Привет мир"),    # Latin p in a Russian word
        ("Telegram @ Pavel", "Telegram @ Pavel"),
        ("Спасибо", "Спасибо"),
    ])
    def test_fix_lookalikes(self, raw, fixed):
        assert _fix_lookalikes(raw) == fixed

    def test_cyrillic_only_letters(self):
        assert _has_cyrillic_only_letters("Спасибо")
        assert _has_cyrillic_only_letters("мама")      # lowercase м has no Latin twin
        assert not _has_cyrillic_only_letters("MCР")   # only a lookalike
        assert not _has_cyrillic_only_letters("hello")


class TestMergeReadings:
    def test_russian_box_takes_cyrillic(self):
        merged = _merge_readings([(BOX, "Cnacn6o", 0.40)], [(BOX, "Спасибо", 0.90)])
        assert merged == [(BOX, "Спасибо", 0.90)]

    def test_english_box_keeps_latin_even_if_cyrillic_is_confident(self):
        merged = _merge_readings([(BOX, "MCP server", 0.80)], [(BOX, "MCР sеrvеr", 0.95)])
        assert merged[0][1] == "MCP server"

    def test_low_confidence_cyrillic_loses(self):
        merged = _merge_readings([(BOX, "Größe", 0.90)], [(BOX, "Гробе", 0.50)])
        assert merged[0][1] == "Größe"

    def test_length_mismatch_falls_back_to_latin(self):
        merged = _merge_readings([(BOX, "hello", 0.9)], [])
        assert merged == [(BOX, "hello", 0.9)]


@pytest.fixture
def fake_easyocr(monkeypatch):
    """Fake easyocr module recording each Reader built."""
    mod = types.ModuleType("easyocr")
    mod.built = []

    def reader(langs, gpu=False, verbose=False, detector=True):
        if "ch_sim" in langs and "de" in langs:
            raise ValueError("Chinese_sim is only compatible with English")
        r = MagicMock(name=f"Reader{langs}")
        r.langs, r.detector = langs, detector
        mod.built.append(r)
        return r

    mod.Reader = reader
    monkeypatch.setitem(sys.modules, "easyocr", mod)
    return mod


def _with_langs(monkeypatch, value):
    monkeypatch.setattr(ocr.settings, "ocr_languages", value)


def test_default_builds_one_english_reader(fake_easyocr, monkeypatch):
    _with_langs(monkeypatch, "en")
    o = OCRExtractor()
    o._ensure_reader()
    assert [r.langs for r in fake_easyocr.built] == [["en"]]
    assert o._reader_cyr is None


def test_cyrillic_builds_second_reader_without_detector(fake_easyocr, monkeypatch):
    _with_langs(monkeypatch, "en,es,de,fr,ru")
    o = OCRExtractor()
    o._ensure_reader()
    assert [r.langs for r in fake_easyocr.built] == [["en", "es", "de", "fr"], ["ru", "en"]]
    assert fake_easyocr.built[1].detector is False


def test_rejected_mix_falls_back_to_english(fake_easyocr, monkeypatch):
    _with_langs(monkeypatch, "en,de,ch_sim")
    o = OCRExtractor()
    o._ensure_reader()
    assert o.is_available
    assert fake_easyocr.built[-1].langs == ["en"]


def test_extract_uses_both_readers_on_shared_boxes(monkeypatch):
    utils = types.ModuleType("easyocr.utils")
    utils.reformat_input = lambda arr: (arr, arr)
    monkeypatch.setitem(sys.modules, "easyocr.utils", utils)
    monkeypatch.setitem(sys.modules, "easyocr", types.ModuleType("easyocr"))

    o = OCRExtractor()
    o._reader, o._reader_cyr = MagicMock(), MagicMock()
    o._reader.detect.return_value = ([[[0, 10, 0, 10]]], [[]])
    o._reader.recognize.return_value = [(BOX, "Cnacn6o", 0.4)]
    o._reader_cyr.recognize.return_value = [(BOX, "Спасибо", 0.9)]

    text, boxes = o.extract_text_with_boxes(Image.new("RGB", (200, 100), "white"))
    assert text == "Спасибо"
    assert o._reader.detect.call_args.kwargs["canvas_size"] == ocr.OCR_CANVAS_SIZE
    o._reader_cyr.detect.assert_not_called()
    o._reader.readtext.assert_not_called()
