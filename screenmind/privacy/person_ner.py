"""
Person-name detection with a small local NER model.

Model: Davlan/distilbert-base-multilingual-cased-ner-hrl, the int8 ONNX export
by Xenova (license afl-3.0). Trained on 10 languages, English among them. Not
on Russian, but its vocabulary covers Cyrillic, and it finds Russian names in
our tests. Only the PER label is used.

Resources (Mac, 1 thread, 400 snapshot frames, measured 2026-10-08):
- 138 MB on disk. The app ships it (see "Where the model comes from").
- About +120 MB memory footprint at load, +165-200 MB in use (it levels
  off; measured over all 1,593 snapshot frames). The weights are saved once
  as external data, which onnxruntime maps from disk. Loaded from the single
  .onnx file, the same model costs about +400 MB.
- CPU 27-46 ms per frame (median 26-30 ms, p90 50-115 ms). Results are cached per line, and
  most lines repeat from frame to frame. One call runs the model for about
  1 s at most; lines over that get the names found so far, and the model on
  a later call.

The tokenizer is privacy/wordpiece.py (plain Python, same output as the
`tokenizers` package), so no extra dependency is needed.

Text is checked line by line. A line without a capital letter is skipped.
The model runs on one thread, under a lock, in batches of 8 windows of up to
64 tokens.

Product names ("Claude", "Slack") and common words at the start of a line
("Ran", "Load") are tagged as names too often. A span is dropped if one of its
words is in _NOT_NAMES, or if it is one word that is also a lowercase word in
the model's vocabulary. That also drops a lone "Mark" or "Will", but not
"Mark Brown". Single words under 3 letters, OCR typos of weekdays and months
("Fridav") and cut-off product names ("Goog") are dropped too. Unknown
products ("Truto") are still taken for names.

Names found once are then matched as plain text on every line, with the
parts of full names: "Mangin" alone is caught after "Aleksandr Mangin".

Where the model comes from, in this order:
1. The app bundle: sys._MEIPASS / BUNDLE_SUBDIR. The PyInstaller build puts
   it there: `python -m screenmind.privacy.person_ner <dir>` installs the
   pinned files into <dir>, and the spec adds <dir> as data at BUNDLE_SUBDIR.
2. ~/.screenmind/models/ner/<MODEL_NAME>.
3. Dev checkouts only: a background download into (2) on first use. Until
   it is ready, names are not detected (the other filters still run). A
   frozen app never downloads; without its bundled model it logs a warning
   and leaves names in.
"""

import hashlib
import logging
import re
import shutil
import subprocess
import sys
import threading
from collections import OrderedDict
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger("screenmind.privacy.person_ner")

MODEL_REPO = "Xenova/distilbert-base-multilingual-cased-ner-hrl"
MODEL_REVISION = "c2a4dbf593c57f47004c5bc2d3770d311aee9c43"
_DOWNLOADS = {
    "onnx/model_int8.onnx": "92977e4b2335a2ee2fb95225e32fd0743a608091856b245c27d9001772023b06",
    "tokenizer.json": "bf1b59b7b11c95f194f51708d918eea378e09d05f84c0e1656dc5180e8117088",
    "config.json": "38847be4dc6699b1218a749ed69f888c2ccc7b4deba98e3c4a1cac8cb34d54c8",
}
# What stays on disk after setup: the model with its weights as external data.
_MODEL = "model.onnx"
_WEIGHTS = "model.onnx.data"

_MAX_TOKENS = 64   # per window; longer lines are split into overlapping windows
_STRIDE = 16
_BATCH = 8
_MIN_PROB = 0.9    # PER probability of a token
_CACHE_LINES = 32768   # a few MB; keeps a long terminal scrollback cached
_MAX_WINDOWS_PER_CALL = 96   # about 1 s of model time
_CHARS_PER_WINDOW = 200
_KNOWN_NAMES = 2048

_CAPITAL = re.compile(r"[A-ZÀ-ÞА-ЯЁ]")
_WORD = re.compile(r"\w+")
_MARKER = re.compile(r"\[REDACTED:[^\]]*\]")

# Products, companies and tools that the model tags as people on work screens.
# Compared lowercase, per word.
_NOT_NAMES = {
    "claude", "anthropic", "opus", "sonnet", "haiku", "fable", "gemma", "gemini",
    "chatgpt", "openai", "gpt", "copilot", "cursor", "llama", "mistral", "qwen",
    "deepseek", "perplexity", "grok", "granola", "slack", "nebius", "tripleten",
    "outlook", "gmail", "google", "chrome", "safari", "firefox", "edge", "apple",
    "mac", "macos", "ios", "windows", "microsoft", "teams", "zoom", "meet",
    "telegram", "whatsapp", "discord", "notion", "linear", "jira", "confluence",
    "atlassian", "rovo", "figma", "miro", "loom", "asana", "trello", "airtable",
    "github", "gitlab", "docker", "python", "jupyter", "vscode", "xcode", "finder",
    "terminal", "iterm", "excel", "powerpoint", "keynote", "postman", "vercel",
    "stripe", "hubspot", "salesforce", "intercom", "zendesk", "calendly",
    "dropbox", "spotify", "youtube", "linkedin", "twitter", "reddit", "amazon",
    "aws", "azure", "huddle", "eval",
}

# OCR misreads these ("Fridav", "Yesterdav"), and the model then tags them.
# A single word one letter away from one of them is not a name.
_DATE_WORDS = {
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "june", "july", "august",
    "september", "october", "november", "december", "yesterday", "today", "tomorrow",
}


def _one_letter_off(word: str, targets) -> bool:
    w = word.lower()
    return any(len(t) == len(w) and sum(a != b for a, b in zip(t, w)) <= 1 for t in targets)


_enabled = True  # tests turn it off; see tests/conftest.py


MODEL_NAME = "distilbert-multilingual-ner-hrl"
BUNDLE_SUBDIR = Path("models") / "ner" / MODEL_NAME
# The files in a model dir. All of them go into the app bundle (138 MB).
FILES = ("config.json", "tokenizer.json", _MODEL, _WEIGHTS)


def models_dir() -> Path:
    """~/.screenmind/models/ner/<MODEL_NAME>, shared by every data dir, like
    the OCR models (engine/ocr.py)."""
    return Path.home() / ".screenmind" / "models" / "ner" / MODEL_NAME


def bundled_dir() -> Optional[Path]:
    """The model inside a frozen app, or None when running from source."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) / BUNDLE_SUBDIR if base else None


def find_model_dir() -> Optional[Path]:
    """The bundled model, else the one in ~/.screenmind/models; None if neither."""
    for d in (bundled_dir(), models_dir()):
        if d is not None and is_installed(d):
            return d
    return None


def is_installed(model_dir: Optional[Path] = None) -> bool:
    d = model_dir or models_dir()
    return all((d / f).exists() for f in FILES)


def _session_options():
    """As the OCR models (engine/ocr.py): no arena, no memory pattern."""
    import onnxruntime as ort

    so = ort.SessionOptions()
    so.log_severity_level = 4
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    so.enable_cpu_mem_arena = False
    so.enable_mem_pattern = False
    so.intra_op_num_threads = 1
    so.inter_op_num_threads = 1
    return so


def _convert(src: str, dst: str):
    """Save the model with its weights as external data (onnxruntime only)."""
    import onnxruntime as ort

    so = _session_options()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
    so.optimized_model_filepath = dst
    so.add_session_config_entry("session.optimized_model_external_initializers_file_name", _WEIGHTS)
    so.add_session_config_entry("session.optimized_model_external_initializers_min_size_in_bytes", "1024")
    ort.InferenceSession(src, so, providers=["CPUExecutionProvider"])


_CONVERT_CODE = (
    "import sys; from screenmind.privacy.person_ner import _convert; _convert(sys.argv[1], sys.argv[2])"
)


def install(model_dir: Optional[Path] = None) -> Path:
    """Download the model, check its hashes and save the weights as external
    data (needs onnxruntime only). Returns the model dir."""
    import httpx

    d = model_dir or models_dir()
    tmp = d.with_name(d.name + ".download")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        for name, sha in _DOWNLOADS.items():
            url = f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/{name}"
            path = tmp / Path(name).name
            h = hashlib.sha256()
            with httpx.stream("GET", url, follow_redirects=True, timeout=60) as r:
                r.raise_for_status()
                with open(path, "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
                        h.update(chunk)
            if h.hexdigest() != sha:
                raise ValueError(f"{name}: sha256 mismatch")

        # Converting holds about 400 MB, and this process would keep that
        # memory. So it runs in a child Python. Only dev checkouts and the
        # app build install; a frozen app ships the converted files.
        subprocess.run(
            [sys.executable, "-c", _CONVERT_CODE, str(tmp / "model_int8.onnx"), str(tmp / _MODEL)],
            check=True, stdin=subprocess.DEVNULL, capture_output=True, timeout=600,
        )
        (tmp / "model_int8.onnx").unlink()

        shutil.rmtree(d, ignore_errors=True)
        tmp.rename(d)
        return d
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class PersonNer:
    """Finds person names in text. Thread-safe; one model run at a time."""

    def __init__(self, model_dir: Path):
        import json

        import onnxruntime as ort
        from screenmind.privacy.wordpiece import WordPiece

        cfg = json.loads((model_dir / "config.json").read_text())
        self._per = sorted(int(i) for i, label in cfg["id2label"].items() if label.endswith("PER"))
        self._tok = WordPiece.from_file(model_dir / "tokenizer.json")
        self._cls = self._tok.token_to_id("[CLS]")
        self._sep = self._tok.token_to_id("[SEP]")
        self._session = ort.InferenceSession(
            str(model_dir / _MODEL), _session_options(), providers=["CPUExecutionProvider"])
        self._inputs = {i.name for i in self._session.get_inputs()}
        self._lock = threading.Lock()
        self._cache: "OrderedDict[str, List[Tuple[int, int]]]" = OrderedDict()
        self._known: "OrderedDict[str, None]" = OrderedDict()  # names found so far
        self._known_re = None

    def find(self, lines: List[str]) -> List[List[Tuple[int, int]]]:
        """Name spans (start, end) per line, after the _NOT_NAMES rules.

        Two sources: the model, and names it found before, matched as plain
        text on every line. The second catches a surname alone ("Mangin")
        once the full name was seen, and lines over the per-call cap.
        """
        out: List[List[Tuple[int, int]]] = [[] for _ in lines]
        todo = []
        with self._lock:
            for i, line in enumerate(lines):
                if not _CAPITAL.search(line):
                    continue
                if line in self._cache:
                    self._cache.move_to_end(line)
                    out[i] = self._cache[line]
                else:
                    todo.append(i)
            if todo:
                # Model time per call is capped; a whole terminal scrollback
                # (250k chars) would take half a minute. Lines over the cap go
                # to the model on a later call.
                now, windows = [], 0
                for line in dict.fromkeys(lines[i] for i in todo):
                    windows += 1 + len(line) // _CHARS_PER_WINDOW
                    if windows > _MAX_WINDOWS_PER_CALL and now:
                        break
                    now.append(line)
                new = dict(zip(now, self._run(now)))
                self._cache.update(new)
                while len(self._cache) > _CACHE_LINES:
                    self._cache.popitem(last=False)
                for line, spans in new.items():
                    for a, b in spans:
                        self._learn(line[a:b])
                for i in todo:
                    out[i] = new.get(lines[i], [])
            if self._known:
                for i, line in enumerate(lines):
                    if _CAPITAL.search(line):
                        out[i] = self._add_known(line, out[i])
        return out

    def _learn(self, name: str):
        """Remember a found name, and the parts of a full name that are not
        common words ("Aleksandr Mangin" -> "Aleksandr", "Mangin")."""
        parts = [name] + [w for w in name.split() if len(w) >= 3 and w != name and self.is_name(w)]
        for p in parts:
            if p not in self._known:
                self._known_re = None
            self._known[p] = None
            self._known.move_to_end(p)
        while len(self._known) > _KNOWN_NAMES:
            self._known.popitem(last=False)
            self._known_re = None

    def _add_known(self, line: str, spans: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
        """spans plus known names in the line that don't overlap them or a marker."""
        if self._known_re is None:
            names = sorted(self._known, key=len, reverse=True)
            self._known_re = re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, names)) + r")(?!\w)")
        taken = list(spans) + [m.span() for m in _MARKER.finditer(line)]
        extra = [m.span() for m in self._known_re.finditer(line)
                 if not any(m.start() < b and a < m.end() for a, b in taken)]
        return sorted(spans + extra) if extra else spans

    def _run(self, lines: List[str]) -> List[List[Tuple[int, int]]]:
        import numpy as np

        # One row per window; a long line has several, overlapping by _STRIDE.
        rows = []  # (line index, ids, offsets)
        size = _MAX_TOKENS - 2
        for i, line in enumerate(lines):
            ids, offsets = self._tok.encode(line)
            for start in range(0, max(1, len(ids) - _STRIDE), size - _STRIDE):
                rows.append((i, [self._cls] + ids[start:start + size] + [self._sep],
                             [(0, 0)] + offsets[start:start + size] + [(0, 0)]))
        rows.sort(key=lambda r: len(r[1]))

        hits: List[List[Tuple[int, int]]] = [[] for _ in lines]
        for s in range(0, len(rows), _BATCH):
            batch = rows[s:s + _BATCH]
            width = max(len(r[1]) for r in batch)
            ids = np.zeros((len(batch), width), dtype=np.int64)
            mask = np.zeros_like(ids)
            for r, (_, row_ids, _) in enumerate(batch):
                ids[r, :len(row_ids)] = row_ids
                mask[r, :len(row_ids)] = 1
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self._inputs:
                feed["token_type_ids"] = np.zeros_like(ids)
            logits = self._session.run(None, feed)[0]
            prob = np.exp(logits - logits.max(-1, keepdims=True))
            prob /= prob.sum(-1, keepdims=True)
            is_per = np.isin(prob.argmax(-1), self._per) & (prob[..., self._per].sum(-1) >= _MIN_PROB)
            for r, (i, row_ids, offsets) in enumerate(batch):
                for t, (a, b) in enumerate(offsets):
                    if b > a and is_per[r, t]:
                        hits[i].append((a, b))

        return [self._spans(line, h) for line, h in zip(lines, hits)]

    def _spans(self, line: str, hits: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
        """Join token hits into words and names, then drop non-names."""
        merged: List[List[int]] = []
        for a, b in sorted(set(hits)):
            if merged and a - merged[-1][1] <= 1:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        spans = []
        markers = [m.span() for m in _MARKER.finditer(line)]
        for a, b in merged:
            while a > 0 and (line[a - 1].isalnum() or (line[a - 1] in "-'’" and a > 1 and line[a - 2].isalnum())):
                a -= 1
            while b < len(line) and (line[b].isalnum() or (line[b] in "-'’" and b + 1 < len(line) and line[b + 1].isalnum())):
                b += 1
            if any(a < mb and ma < b for ma, mb in markers):
                continue
            if self.is_name(line[a:b]):
                spans.append((a, b))
        return spans

    def is_name(self, span: str) -> bool:
        words = _WORD.findall(span)
        if not words or not span[0].isupper():
            return False
        if any(w.lower() in _NOT_NAMES for w in words):
            return False
        if len(words) == 1:
            w = span.lower()
            if len(span) < 3 or self._tok.token_to_id(w) is not None:
                return False  # "DM"; common words ("Ran", "Mark")
            if _one_letter_off(w, _DATE_WORDS):
                return False
            if len(w) >= 4 and any(n.startswith(w) for n in _NOT_NAMES):
                return False  # cut-off product name ("Goog")
        return True


_detector: Optional[PersonNer] = None
_state_lock = threading.Lock()
_installing = False
_failed = False


def _install_in_background():
    global _installing, _failed
    try:
        logger.info(f"Downloading the person-name model ({MODEL_REPO}, 135 MB)...")
        install()
        logger.info("Person-name model ready")
    except Exception as e:
        _failed = True
        logger.warning(f"Person-name model download failed; names are not filtered: {e}")
    finally:
        _installing = False


def get_detector() -> Optional[PersonNer]:
    """The loaded detector, or None while it is not available. In a dev
    checkout, the first call starts the download if the model is missing."""
    global _detector, _installing, _failed
    if _detector is not None or not _enabled or _failed:
        return _detector
    with _state_lock:
        if _detector is not None or _failed:
            return _detector
        d = find_model_dir()
        if d is not None:
            try:
                _detector = PersonNer(d)
                logger.info(f"Person-name filter loaded from {d}")
            except Exception as e:
                _failed = True
                logger.warning(f"Person-name model failed to load; names are not filtered: {e}")
        elif getattr(sys, "frozen", False):
            _failed = True
            logger.warning(f"Person-name model missing from the app ({bundled_dir()}); names are not filtered")
        elif not _installing:
            _installing = True
            threading.Thread(target=_install_in_background, name="ner-install", daemon=True).start()
    return _detector


def set_detector(detector, enabled: bool = True):
    """For tests: use this detector (or None), and turn loading on or off."""
    global _detector, _enabled
    _detector = detector
    _enabled = enabled


def redact_persons(text: str, replacement: str) -> Tuple[str, int]:
    """Replace person names. Returns (text, count). A no-op while the model
    is not available."""
    detector = get_detector()
    if detector is None or not text:
        return text, 0
    lines = text.split("\n")
    count = 0
    for i, spans in enumerate(detector.find(lines)):
        if not spans:
            continue
        line = lines[i]
        for a, b in reversed(spans):
            line = line[:a] + replacement + line[b:]
        lines[i] = line
        count += len(spans)
    return "\n".join(lines), count


if __name__ == "__main__":
    # For the app build: python -m screenmind.privacy.person_ner <dir>
    # Downloads the pinned files (sha256 checked) and converts them into <dir>.
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else models_dir()
    if is_installed(target):
        logger.info(f"already installed: {target}")
    else:
        logger.info(f"installed: {install(target)}")
