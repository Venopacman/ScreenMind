"""Tests for engine/analyzer.py — response parsing logic (no Ollama needed)."""

from screenmind.engine.analyzer import GemmaAnalyzer


def test_parse_clean_json():
    analyzer = GemmaAnalyzer()
    raw = '{"app_name": "Chrome", "activity_category": "browsing", "activity_summary": "Reading docs", "detailed_context": "", "visible_text_snippets": [], "mood": "learning", "confidence": 0.9, "scene_description": ""}'
    record = analyzer._parse_response(raw)
    assert record.app_name == "Chrome"
    assert record.activity_category == "browsing"
    assert record.mood == "learning"


def test_parse_json_in_code_block():
    analyzer = GemmaAnalyzer()
    raw = '```json\n{"app_name": "VS Code", "activity_category": "coding", "activity_summary": "Editing main.py", "detailed_context": "", "visible_text_snippets": [], "mood": "productive", "confidence": 0.85, "scene_description": ""}\n```'
    record = analyzer._parse_response(raw)
    assert record.app_name == "VS Code"
    assert record.activity_category == "coding"


def test_parse_with_thinking_tags():
    analyzer = GemmaAnalyzer()
    raw = '<think>Let me analyze this screenshot...</think>{"app_name": "Slack", "activity_category": "communication", "activity_summary": "Chatting", "detailed_context": "", "visible_text_snippets": [], "mood": "collaborative", "confidence": 0.8, "scene_description": ""}'
    record = analyzer._parse_response(raw)
    assert record.app_name == "Slack"
    assert record.activity_category == "communication"


def test_parse_regex_fallback():
    analyzer = GemmaAnalyzer()
    raw = 'Here is the analysis: "app_name": "Terminal", "activity_category": "terminal", "activity_summary": "running tests"'
    record = analyzer._parse_response(raw)
    # Regex fallback should extract what it can
    assert record.confidence == 0.3  # Low confidence for regex


def test_normalize_category():
    analyzer = GemmaAnalyzer()
    from screenmind.storage.models import ActivityRecord
    # The normalize function checks if a valid category is a substring
    record = ActivityRecord(activity_category="browsing", mood="productive")
    normalized = analyzer._normalize(record)
    assert normalized.activity_category == "browsing"
    assert normalized.mood == "productive"


def test_normalize_invalid_category():
    analyzer = GemmaAnalyzer()
    from screenmind.storage.models import ActivityRecord
    record = ActivityRecord(activity_category="invalid_thing", mood="unknown_mood")
    normalized = analyzer._normalize(record)
    assert normalized.activity_category == "other"
    assert normalized.mood == "neutral"


def test_normalize_confidence_clamping():
    analyzer = GemmaAnalyzer()
    from screenmind.storage.models import ActivityRecord
    # Pydantic enforces 0-1 range, so test that normalize handles edge values
    record = ActivityRecord(confidence=1.0)
    normalized = analyzer._normalize(record)
    assert normalized.confidence == 1.0

    record = ActivityRecord(confidence=0.0)
    normalized = analyzer._normalize(record)
    assert normalized.confidence == 0.0


def test_gemma_text_hint_trimmed_in_screen_order():
    from screenmind.engine import analyzer
    top = "zebra crossing report"
    text = top + "\n" + "\n".join(f"word{i:05d}" for i in range(5000))
    hint = analyzer._text_hint(text)
    assert len(hint) <= analyzer._GEMMA_TEXT_HINT_CHARS
    assert "zebra" in hint  # top of the screen survives the trim, despite sorting last
    assert "word04999" not in hint
