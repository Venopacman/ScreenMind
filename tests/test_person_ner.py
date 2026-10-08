"""Person-name filter (privacy/person_ner.py).

Rule tests use a fake detector, so they need no model. The model tests run
only when the model is on disk (SCREENMIND_NER_MODEL_DIR, or the default
~/.screenmind/models/ner/...); they never download it.
"""

import os
import re
import threading
from collections import OrderedDict
from pathlib import Path
from unittest.mock import patch

import pytest

from screenmind.config import settings
from screenmind.privacy import person_ner
from screenmind.privacy.data_filter import filter_ocr_boxes, filter_sensitive_text
from screenmind.storage.database import Database
from screenmind.workers import audio_worker as aw


class FakeDetector:
    """Marks the given names wherever they occur."""

    def __init__(self, *names):
        self.names = names
        self.calls = 0

    def find(self, lines):
        self.calls += 1
        out = []
        for line in lines:
            spans = []
            for n in self.names:
                spans += [m.span() for m in re.finditer(re.escape(n), line)]
            out.append(sorted(spans))
        return out


@pytest.fixture
def detector():
    def use(*names):
        d = FakeDetector(*names)
        person_ner.set_detector(d)
        return d
    yield use
    person_ner.set_detector(None, enabled=False)


# ── Wiring, with a fake detector ─────────────────────────────────────

def test_names_replaced_on_every_line(detector):
    detector("Ivan Petrov", "Анна")
    text = "Ivan Petrov  10:42 AM\nfix the build\nАнна: привет, Ivan Petrov"
    r = filter_sensitive_text(text)
    assert r["clean_text"] == (
        "[REDACTED:person]  10:42 AM\nfix the build\n[REDACTED:person]: привет, [REDACTED:person]")
    assert {"type": "person", "count": 3} in r["details"]


def test_person_runs_after_email(detector):
    # The detector sees the email marker, not "ivan.petrov@...".
    d = detector("ivan.petrov")
    r = filter_sensitive_text("mail ivan.petrov@nebius.com")
    assert r["clean_text"] == "mail [REDACTED:email@nebius.com]"
    assert d.calls == 1


def test_person_off_when_not_in_types(detector):
    detector("Ivan Petrov")
    assert filter_sensitive_text("Ivan Petrov", ["email"])["clean_text"] == "Ivan Petrov"


def test_no_detector_leaves_names():
    person_ner.set_detector(None, enabled=False)
    assert filter_sensitive_text("Ivan Petrov")["clean_text"] == "Ivan Petrov"


def test_ocr_boxes(detector):
    detector("Maria Garcia")
    boxes = [{"text": "Maria Garcia", "box": [0, 0, 1, 1]}, {"text": "deploy done"}]
    filter_ocr_boxes(boxes)
    assert [b["text"] for b in boxes] == ["[REDACTED:person]", "deploy done"]


def test_export_manifest_lists_person_only_with_model(detector, monkeypatch):
    from screenmind.export.archive import filter_types
    person_ner.set_detector(None, enabled=False)
    assert "person" not in filter_types()
    detector("x")
    assert "person" in filter_types()


def test_meeting_title_transcript_and_summary_filtered(detector, tmp_path, monkeypatch):
    detector("Ivan Petrov")
    db = Database(db_path=tmp_path / "test.db")
    monkeypatch.setattr(settings, "meeting_transcription", False)
    monkeypatch.setattr(settings, "meeting_apps", "meet")
    meet = {"owner": "Google Chrome", "title": "Meet - 1:1 Ivan Petrov", "pid": 20, "bounds": (0, 0, 800, 600)}
    with patch.object(aw.AudioWorker, "_init_transcription"), \
            patch("screenmind.capture.window.get_window_url", return_value=None):
        worker = aw.AudioWorker(database=db)
        worker.update([meet], None, now=1000.0)
        worker.update([meet], None, now=1005.0)
        [row] = [dict(r) for r in db._get_conn().execute("SELECT * FROM meetings")]
        assert row["window_title"] == "Meet - 1:1 [REDACTED:person]"

        # A restart still finds the same call: the live title is filtered too.
        worker.force_stop()
        restarted = aw.AudioWorker(database=db)
        restarted.update([meet], None, now=1010.0)
        restarted.update([meet], None, now=1015.0)
        assert db._get_conn().execute("SELECT COUNT(*) FROM meetings").fetchone()[0] == 1

    assert aw._clean("Ivan Petrov said hi") == "[REDACTED:person] said hi"


# ── Span rules, with a fake tokenizer ────────────────────────────────

class _Tok:
    vocab = {"ran", "load", "mark", "code"}

    def token_to_id(self, t):
        return 1 if t in self.vocab else None


def _bare():
    ner = object.__new__(person_ner.PersonNer)
    ner._tok = _Tok()
    ner._lock = threading.Lock()
    ner._cache = OrderedDict()
    ner._known = OrderedDict()
    ner._known_re = None
    return ner


@pytest.mark.parametrize("span,ok", [
    ("Ivan Petrov", True),
    ("Pavel", True),
    ("Fatima Al-Sayed", True),
    ("Сергей", True),
    ("Mark Brown", True),
    ("Mark", False),          # lowercase word in the vocab
    ("Ran", False),
    ("Claude", False),        # product
    ("Claude Code", False),
    ("Slack Huddle", False),
    ("Atlassian Rovo", False),
    ("ivan", False),          # names start with a capital
    ("X", False),
    ("DM", False),
    ("Ian", True),
    ("Fridav", False),        # OCR typo of a weekday
    ("Yesterdav", False),
    ("Goog", False),          # cut-off product name
    ("Olga", True),
])
def test_is_name(span, ok):
    assert _bare().is_name(span) is ok


def test_spans_grow_to_words_and_skip_markers():
    ner = _bare()
    line = "Ivan Petrov wrote to [REDACTED:email@nebius.com] and Al-Sayed"
    # Word pieces, as the tokenizer gives them; one hit inside the marker
    hits = [(0, 4), (5, 8), (8, 11), (31, 34), (53, 55), (55, 56), (56, 59)]
    spans = ner._spans(line, hits)
    assert [line[a:b] for a, b in spans] == ["Ivan Petrov", "Al-Sayed"]


def test_model_time_per_call_is_capped(monkeypatch):
    ner = _bare()
    ran = []

    def run(lines):
        ran.append(list(lines))
        return [[(0, 11)] if line.startswith("Ivan Petrov") else [] for line in lines]

    ner._run = run
    monkeypatch.setattr(person_ner, "_MAX_WINDOWS_PER_CALL", 2)
    lines = ["Ivan Petrov hi", "Line B", "Line C mentions Ivan Petrov", "No names"]
    out = ner.find(lines)
    assert ran == [["Ivan Petrov hi", "Line B"]]
    # Over the cap: names found so far, matched as text
    assert out[2] == [(16, 27)] and out[3] == []

    # Lines over the cap were not cached, so the model gets them next time.
    ner.find(lines)
    assert ran[-1] == ["Line C mentions Ivan Petrov", "No names"]
    assert ner.find(lines) == [[(0, 11)], [], [(16, 27)], []]
    assert len(ran) == 2


def test_known_names_and_their_parts_match_everywhere():
    ner = _bare()
    ner._run = lambda lines: [[(0, 16)] if line.startswith("Aleksandr Mangin") else [] for line in lines]
    ner.find(["Aleksandr Mangin joined"])
    # The model found nothing in these lines; the known name and its parts match.
    assert ner.find(["Kick off [Ivan + Mangin]", "Ask Aleksandr Mangin", "Mangina Street", "Load Mark"]) == [
        [(17, 23)], [(4, 20)], [], []]
    # A part that is a common word ("Mark") is not learned.
    ner.find(["Mark Mangin"])
    assert ner._known.keys() >= {"Aleksandr Mangin", "Aleksandr", "Mangin"}
    assert "Mark" not in ner._known


def test_cache_skips_the_model_for_seen_lines():
    ner = _bare()
    ran = []
    ner._run = lambda lines: ran.append(lines) or [[] for _ in lines]
    ner.find(["Hello World", "lowercase only", "Hello World"])
    ner.find(["Hello World"])
    assert ran == [["Hello World"]]


# ── The real model, when it is on disk ───────────────────────────────

def _model_dir():
    d = os.environ.get("SCREENMIND_NER_MODEL_DIR")
    return Path(d) if d else person_ner.models_dir()


needs_model = pytest.mark.skipif(not person_ner.is_installed(_model_dir()), reason="NER model not downloaded")

# Synthetic lines in the forms seen on work screens (Slack, Linear, mail).
POSITIVE = [
    ("Ivan Petrov  10:42 AM", ["Ivan Petrov"]),
    ("@Maria Garcia can you check the deploy?", ["Maria Garcia"]),
    ("Thanks, John! I will take a look tomorrow", ["John"]),
    ("Assigned to Priya Sharma", ["Priya Sharma"]),
    ("Анна Кузнецова: привет, посмотри PR", ["Анна Кузнецова"]),
    ("Дмитрий Соколов добавил вас в канал", ["Дмитрий Соколов"]),
    ("Meeting with Sarah Johnson and Tom Baker", ["Sarah Johnson", "Tom Baker"]),
    ("Klaus Müller commented on ENG-412", ["Klaus Müller"]),
    ("Reviewed by Wei Zhang", ["Wei Zhang"]),
    ("Olga Ivanova reacted with :+1:", ["Olga Ivanova"]),
    ("Hi Elena, the report is ready", ["Elena"]),
    ("From: Michael Brown", ["Michael Brown"]),
    ("Carlos Mendoza 3:15 PM  pushed 2 commits", ["Carlos Mendoza"]),
    ("Сергей, созвонимся в 15:00?", ["Сергей"]),
    ("Fatima Al-Sayed joined the channel", ["Fatima Al-Sayed"]),
    ("Owner: Jessica Lee", ["Jessica Lee"]),
    ("Екатерина Смирнова  12:01", ["Екатерина Смирнова"]),
    ("Nikolai Volkov started a huddle", ["Nikolai Volkov"]),
    ("Daniel Wilson, Emma Davis and 3 others", ["Daniel Wilson", "Emma Davis"]),
    ("Lucas Silva approved these changes", ["Lucas Silva"]),
]
NEGATIVE = [
    "Google Chrome", "Pull request #123 merged into main", "Visual Studio Code",
    "New York Times", "Sprint Review", "Slack Huddle", "Microsoft Teams",
    "Claude Code", "Load Balancer", "Mark as done", "Product Requirements Document",
    "Settings > Privacy & Security", "Customer Success Team", "Monday, October 6",
    "Активность за неделю", "Новое сообщение", "Москва, Россия", "Docker Desktop",
    "Weekly sync notes", "Jira Service Management", "Ran 12 tests in 0.4s",
]


@pytest.fixture(scope="module")
def model():
    return person_ner.PersonNer(_model_dir())


@needs_model
def test_model_recall_and_precision(model):
    found = model.find([t for t, _ in POSITIVE] + NEGATIVE)
    tp = fn = fp = 0
    for (text, gold), spans in zip(POSITIVE, found):
        got = {text[a:b] for a, b in spans}
        tp += len(set(gold) & got)
        fn += len(set(gold) - got)
        fp += len(got - set(gold))
    fp += sum(len(s) for s in found[len(POSITIVE):])
    recall, precision = tp / (tp + fn), tp / (tp + fp)
    # Measured 2026-10-08: recall 1.00, precision 1.00 on this set.
    assert recall >= 0.9, (recall, found)
    assert precision >= 0.9, (precision, found)


@needs_model
def test_model_on_real_looking_text(model):
    person_ner.set_detector(model)
    try:
        text = "\n".join([
            "", "   ", "#general", "x" * 3000,
            # A long a11y paragraph: the name is past the first model window.
            "The build failed again because the cache was stale. " * 20 + "Ping Olga Ivanova about it.",
            "def foo(): return {'a': 1}", "😀 Reaction from Sasha", "[REDACTED:email@nebius.com]",
            "​ ", "ID 4111-1111-1111-1111", "Ivan Petrov  10:42 AM",
        ])
        clean = filter_sensitive_text(text)["clean_text"]
        assert len(clean.split("\n")) == len(text.split("\n"))
        assert "Ivan Petrov" not in clean and "Olga Ivanova" not in clean
        assert "[REDACTED:email@nebius.com]" in clean
        assert "#general" in clean
    finally:
        person_ner.set_detector(None, enabled=False)
