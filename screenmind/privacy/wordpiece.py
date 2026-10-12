"""
BERT WordPiece tokenizer (cased) in plain Python, for privacy/person_ner.py.

It reads the model's tokenizer.json and gives the same ids and character
offsets as the `tokenizers` package with that file (BertNormalizer with
lowercase off, BertPreTokenizer, WordPiece). Checked on every line of the EDA
snapshot. Using it saves `tokenizers` and the huggingface-hub packages it
pulls in. Special tokens typed in the text ("[MASK]") are split like other
text; `tokenizers` would map them to their ids.
"""

import json
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def _is_control(ch: str) -> bool:
    if ch in "\t\n\r":
        return False
    return unicodedata.category(ch).startswith("C")


def _is_whitespace(ch: str) -> bool:
    return ch in " \t\n\r" or unicodedata.category(ch) == "Zs"


def _is_punctuation(ch: str) -> bool:
    o = ord(ch)
    if 33 <= o <= 47 or 58 <= o <= 64 or 91 <= o <= 96 or 123 <= o <= 126:
        return True
    return unicodedata.category(ch).startswith("P")


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0x20000 <= o <= 0x2A6DF
            or 0x2A700 <= o <= 0x2B73F or 0x2B740 <= o <= 0x2B81F or 0x2B820 <= o <= 0x2CEAF
            or 0xF900 <= o <= 0xFAFF or 0x2F800 <= o <= 0x2FA1F)


class WordPiece:
    def __init__(self, vocab: Dict[str, int], unk: str = "[UNK]", prefix: str = "##",
                 max_chars: int = 100):
        self.vocab = vocab
        self.unk_id = vocab[unk]
        self.prefix = prefix
        self.max_chars = max_chars

    @classmethod
    def from_file(cls, path: Path) -> "WordPiece":
        model = json.loads(Path(path).read_text(encoding="utf-8"))["model"]
        return cls(model["vocab"], model.get("unk_token", "[UNK]"),
                   model.get("continuing_subword_prefix", "##"),
                   model.get("max_input_chars_per_word", 100))

    def token_to_id(self, token: str) -> Optional[int]:
        return self.vocab.get(token)

    def _words(self, text: str) -> List[List[Tuple[str, int]]]:
        """Words as (char, offset) lists: split on whitespace, every
        punctuation or CJK char on its own, control chars dropped."""
        words, cur = [], []
        for i, ch in enumerate(text):
            if ch == "\0" or ch == "�" or _is_control(ch):
                continue
            if _is_whitespace(ch):
                if cur:
                    words.append(cur)
                    cur = []
            elif _is_punctuation(ch) or _is_cjk(ch):
                if cur:
                    words.append(cur)
                    cur = []
                words.append([(ch, i)])
            else:
                cur.append((ch, i))
        if cur:
            words.append(cur)
        return words

    def encode(self, text: str) -> Tuple[List[int], List[Tuple[int, int]]]:
        """Token ids and (start, end) offsets into text, without [CLS]/[SEP]."""
        ids, offsets = [], []
        for word in self._words(text):
            chars = "".join(c for c, _ in word)
            start_off, end_off = word[0][1], word[-1][1] + 1
            if len(chars) > self.max_chars:
                ids.append(self.unk_id)
                offsets.append((start_off, end_off))
                continue
            pieces, start = [], 0
            while start < len(chars):
                end = len(chars)
                while end > start:
                    sub = chars[start:end] if start == 0 else self.prefix + chars[start:end]
                    tid = self.vocab.get(sub)
                    if tid is not None:
                        pieces.append((tid, start, end))
                        break
                    end -= 1
                else:
                    pieces = None
                    break
                start = end
            if pieces is None:
                ids.append(self.unk_id)
                offsets.append((start_off, end_off))
                continue
            for tid, a, b in pieces:
                ids.append(tid)
                offsets.append((word[a][1], word[b - 1][1] + 1))
        return ids, offsets
