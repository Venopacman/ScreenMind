"""Tests for OCR language handling and result parsing (RapidOCR is faked)."""
import sys
import types
from enum import Enum

import numpy as np
import pytest
from PIL import Image

from screenmind.config import Settings
from screenmind.engine import ocr
from screenmind.engine.ocr import OCRExtractor, _fix_lookalikes, _rec_model

BOX = [[0.0, 0.0], [10.5, 0.0], [10.5, 10.0], [0.0, 10.0]]


class TestDefaultLanguages:
    """G32: without OCR_LANGUAGES every machine reads the same scripts."""

    def _defaults(self, monkeypatch, **kw):
        monkeypatch.delenv("OCR_LANGUAGES", raising=False)
        return Settings(_env_file=None, data_dir="/tmp/test", **kw)

    def test_default_reads_latin_and_cyrillic(self, monkeypatch):
        s = self._defaults(monkeypatch)
        assert s.ocr_languages_list == ["en", "es", "de", "fr", "ru"]
        # The one recognizer that also reads English and accented Latin
        assert _rec_model(s.ocr_languages_list) == "eslav"

    def test_empty_value_means_default(self, monkeypatch):
        s = self._defaults(monkeypatch, ocr_languages=" , ")
        assert s.ocr_languages_list == ["en", "es", "de", "fr", "ru"]

    def test_env_still_overrides(self, monkeypatch):
        monkeypatch.setenv("OCR_LANGUAGES", "en")
        s = Settings(_env_file=None, data_dir="/tmp/test")
        assert s.ocr_languages_list == ["en"]
        assert _rec_model(s.ocr_languages_list) == "en"


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

    def test_mixed_scripts_prefer_cyrillic(self):
        assert _rec_model(["en", "ru", "es"]) == "eslav"
        # Cyrillic models also read Latin, so they win over the Latin model
        assert _rec_model(["en", "es", "ru"]) == "eslav"
        assert _rec_model(["en", "es", "de", "fr", "ru"]) == "eslav"
        assert _rec_model(["en", "es", "bg"]) == "cyrillic"
        # Without a Cyrillic model the first code still wins
        assert _rec_model(["en", "ja", "ko"]) == "ch"

    def test_warns_only_when_a_script_is_dropped(self, caplog):
        with caplog.at_level("WARNING", logger="screenmind.engine.ocr"):
            _rec_model(["en", "es", "de", "fr", "ru"])  # Latin is read by eslav
        assert not caplog.records
        with caplog.at_level("WARNING", logger="screenmind.engine.ocr"):
            _rec_model(["en", "ru", "ja"])
        assert "one script at a time" in caplog.text


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


def test_default_engine_reads_cyrillic(fake_rapidocr, monkeypatch):
    # The Windows case of G32: "Работа" came out as "Pa6ota" with the English model
    _with_langs(monkeypatch, Settings.model_fields["ocr_languages"].default)
    fake_rapidocr.result = types.SimpleNamespace(
        boxes=np.array([BOX, BOX]), txts=("Работа", "MCР server"), scores=(0.9, 0.9))
    o = OCRExtractor()
    text = o.extract_text(Image.new("RGB", (20, 20)))
    assert fake_rapidocr.built[0].params["Rec.lang_type"].value == "eslav"
    assert text == "Работа\nMCP server"


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


class TestOnnxOptions:
    def test_engine_gets_threads_and_no_arena(self, fake_rapidocr, monkeypatch):
        _with_langs(monkeypatch, "en")
        monkeypatch.setattr(ocr.settings, "ocr_threads", 2)
        OCRExtractor()._ensure_reader()
        p = fake_rapidocr.built[0].params
        assert p["EngineConfig.onnxruntime.intra_op_num_threads"] == 2
        assert p["EngineConfig.onnxruntime.enable_cpu_mem_arena"] is False

    def test_zero_threads_means_onnxruntime_default(self, fake_rapidocr, monkeypatch):
        _with_langs(monkeypatch, "en")
        monkeypatch.setattr(ocr.settings, "ocr_threads", 0)
        OCRExtractor()._ensure_reader()
        assert fake_rapidocr.built[0].params["EngineConfig.onnxruntime.intra_op_num_threads"] == -1

    def test_session_options(self):
        so = ocr._session_options(2)
        assert so.enable_mem_pattern is False
        assert so.enable_cpu_mem_arena is False
        assert so.intra_op_num_threads == 2
        assert ocr._session_options(0).intra_op_num_threads == 0  # onnxruntime picks

    def test_sessions_rebuilt_with_our_options(self, fake_rapidocr, monkeypatch):
        import onnxruntime as ort

        built = []

        class FakeSession:
            def __init__(self, path, sess_options=None, providers=None):
                self._model_path = path
                self.options = sess_options
                self.providers = providers
                built.append(self)

            def get_providers(self):
                return ["CPUExecutionProvider"]

        def part(name):
            return types.SimpleNamespace(session=types.SimpleNamespace(
                session=FakeSession(f"/models/{name}.onnx")))

        class Engine(fake_rapidocr.RapidOCR):
            def __init__(self, params):
                super().__init__(params)
                self.text_det, self.text_cls, self.text_rec = part("det"), part("cls"), part("rec")

        monkeypatch.setattr(fake_rapidocr, "RapidOCR", Engine)
        monkeypatch.setattr(ort, "InferenceSession", FakeSession)
        _with_langs(monkeypatch, "en")
        monkeypatch.setattr(ocr.settings, "ocr_threads", 3)
        o = OCRExtractor()
        o._ensure_reader()

        sessions = [o._engine.text_det, o._engine.text_cls, o._engine.text_rec]
        rebuilt = [s.session.session for s in sessions]
        assert [s._model_path for s in rebuilt] == ["/models/det.onnx", "/models/cls.onnx", "/models/rec.onnx"]
        for s in rebuilt:
            assert s.options.enable_mem_pattern is False
            assert s.options.intra_op_num_threads == 3
            assert s.providers == ["CPUExecutionProvider"]

    def test_tuning_failure_keeps_ocr_working(self, fake_rapidocr, monkeypatch):
        # The fake engine has no text_det etc., so tuning fails.
        _with_langs(monkeypatch, "en")
        o = OCRExtractor()
        o._ensure_reader()
        assert o.is_available and o._engine is not None
