"""Tests for engine/layout_analyzer.py — pure geometry logic."""
import pytest
from screenmind.engine.layout_analyzer import (
    _is_timestamp,
    _group_into_rows,
    _format_rows_simple,
    _format_chat_messages,
    cluster_ocr_layout,
    organize_ocr_text,
)


# ── _is_timestamp ───────────────────────────────────────────────────────

class TestIsTimestamp:
    def test_date_format_dd_mm_yyyy(self):
        assert _is_timestamp("02-12-2024 20:02") is True

    def test_date_format_slashes(self):
        assert _is_timestamp("02/12/2024") is True

    def test_date_format_dots(self):
        assert _is_timestamp("02.12.2024") is True

    def test_not_a_timestamp(self):
        assert _is_timestamp("Hello world") is False

    def test_short_date(self):
        assert _is_timestamp("02-12-24") is True


# ── _group_into_rows ────────────────────────────────────────────────────

class TestGroupIntoRows:
    def test_empty_boxes(self):
        assert _group_into_rows([]) == []

    def test_single_row(self):
        boxes = [
            {"box": [[0, 100], [50, 100], [50, 120], [0, 120]], "text": "A"},
            {"box": [[60, 102], [110, 102], [110, 122], [60, 122]], "text": "B"},
        ]
        rows = _group_into_rows(boxes)
        assert len(rows) == 1
        assert len(rows[0]) == 2

    def test_two_rows(self):
        boxes = [
            {"box": [[0, 100], [50, 100], [50, 120], [0, 120]], "text": "A"},
            {"box": [[0, 200], [50, 200], [50, 220], [0, 220]], "text": "B"},
        ]
        rows = _group_into_rows(boxes)
        assert len(rows) == 2


# ── _format_rows_simple ────────────────────────────────────────────────

def test_format_rows_simple():
    boxes = [
        {"box": [[0, 100], [50, 100], [50, 120], [0, 120]], "text": "Hello"},
        {"box": [[60, 100], [120, 100], [120, 120], [60, 120]], "text": "World"},
    ]
    result = _format_rows_simple(boxes)
    assert "Hello" in result
    assert "World" in result


# ── _format_chat_messages ───────────────────────────────────────────────

class TestFormatChatMessages:
    def test_empty_boxes(self):
        assert _format_chat_messages([]) == ""

    def test_with_sender_and_timestamp(self):
        boxes = [
            {"box": [[10, 100], [80, 100], [80, 120], [10, 120]], "text": "Alice"},
            {"box": [[90, 100], [200, 100], [200, 120], [90, 120]], "text": "02-12-2024 10:30"},
            {"box": [[10, 130], [300, 130], [300, 150], [10, 150]], "text": "Hey how are you?"},
        ]
        result = _format_chat_messages(boxes)
        assert "Alice" in result
        assert "Hey how are you?" in result

    def test_no_timestamps_fallback_to_rows(self):
        boxes = [
            {"box": [[10, 100], [200, 100], [200, 120], [10, 120]], "text": "Just some text"},
            {"box": [[10, 200], [200, 200], [200, 220], [10, 220]], "text": "More text"},
        ]
        result = _format_chat_messages(boxes)
        assert "Just some text" in result


# ── cluster_ocr_layout ─────────────────────────────────────────────────

class TestClusterOcrLayout:
    def test_empty_boxes(self):
        assert cluster_ocr_layout([], 1920, 1080) == []

    def test_center_only(self):
        boxes = [
            {"box": [[500, 100], [700, 100], [700, 120], [500, 120]], "text": "Center1"},
            {"box": [[500, 200], [700, 200], [700, 220], [500, 220]], "text": "Center2"},
            {"box": [[500, 300], [700, 300], [700, 320], [500, 320]], "text": "Center3"},
        ]
        regions = cluster_ocr_layout(boxes, 1920, 1080)
        assert len(regions) >= 1
        assert any(r["content_type"] == "content" for r in regions)

    def test_left_sidebar_detected(self):
        # Put boxes in the left 20% of screen
        boxes = [
            {"box": [[10, 100], [100, 100], [100, 120], [10, 120]], "text": "Nav1"},
            {"box": [[10, 200], [100, 200], [100, 220], [10, 220]], "text": "Nav2"},
            {"box": [[10, 300], [100, 300], [100, 320], [10, 320]], "text": "Nav3"},
            # Center content
            {"box": [[500, 100], [800, 100], [800, 120], [500, 120]], "text": "Content1"},
            {"box": [[500, 200], [800, 200], [800, 220], [500, 220]], "text": "Content2"},
        ]
        regions = cluster_ocr_layout(boxes, 1920, 1080)
        names = [r["name"] for r in regions]
        assert "region_left" in names

    def test_single_box_returns_fallback(self):
        boxes = [{"box": [[500, 100], [700, 100], [700, 120], [500, 120]], "text": "Alone"}]
        regions = cluster_ocr_layout(boxes, 1920, 1080)
        assert len(regions) == 1
        assert regions[0]["name"] == "main_content"


# ── organize_ocr_text ───────────────────────────────────────────────────

class TestOrganizeOcrText:
    def test_empty_inputs(self):
        assert organize_ocr_text([], [], 1920, 1080) == ""
        assert organize_ocr_text([], [{"name": "x", "x_start": 0, "x_end": 1}], 1920, 1080) == ""

    def test_basic_organization(self):
        regions = [
            {"name": "sidebar", "x_start": 0.0, "x_end": 0.2, "y_start": 0.0, "y_end": 1.0, "content_type": "navigation"},
            {"name": "main", "x_start": 0.2, "x_end": 1.0, "y_start": 0.0, "y_end": 1.0, "content_type": "content"},
        ]
        boxes = [
            {"box": [[50, 100], [100, 100], [100, 120], [50, 120]], "text": "Home", "conf": 0.9},
            {"box": [[500, 100], [800, 100], [800, 120], [500, 120]], "text": "Article text here", "conf": 0.9},
        ]
        result = organize_ocr_text(boxes, regions, 1920, 1080)
        assert "[SIDEBAR]" in result
        assert "[MAIN]" in result
        assert "Home" in result
        assert "Article text here" in result

    def test_low_confidence_filtered(self):
        regions = [{"name": "all", "x_start": 0, "x_end": 1, "y_start": 0, "y_end": 1, "content_type": "content"}]
        boxes = [
            {"box": [[10, 10], [100, 10], [100, 30], [10, 30]], "text": "Good", "conf": 0.9},
            {"box": [[10, 50], [100, 50], [100, 70], [10, 70]], "text": "Bad", "conf": 0.1},
        ]
        result = organize_ocr_text(boxes, regions, 1920, 1080)
        assert "Good" in result
        assert "Bad" not in result

    def test_single_char_filtered(self):
        regions = [{"name": "all", "x_start": 0, "x_end": 1, "y_start": 0, "y_end": 1, "content_type": "content"}]
        boxes = [
            {"box": [[10, 10], [100, 10], [100, 30], [10, 30]], "text": "Hello", "conf": 0.9},
            {"box": [[10, 50], [100, 50], [100, 70], [10, 70]], "text": "x", "conf": 0.9},
        ]
        result = organize_ocr_text(boxes, regions, 1920, 1080)
        assert "Hello" in result
        assert result.count("x") == 0 or "x" not in result.split()
