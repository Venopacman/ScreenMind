"""privacy/wordpiece.py: same ids and offsets as `tokenizers` gives for a
cased BERT tokenizer.json (checked on the whole EDA snapshot, 0 differences).
These tests pin the rules with a tiny vocab."""

from screenmind.privacy.wordpiece import WordPiece

VOCAB = {t: i for i, t in enumerate(
    ["[PAD]", "[UNK]", "[CLS]", "[SEP]", "Iv", "##an", "Pet", "##rov", ",", ":", "hi", "日", "本", "-", "caf", "##é"])}


def test_words_pieces_and_offsets():
    wp = WordPiece(VOCAB)
    ids, offs = wp.encode("Ivan Petrov, hi")
    assert ids == [4, 5, 6, 7, 8, 10]
    assert offs == [(0, 2), (2, 4), (5, 8), (8, 11), (11, 12), (13, 15)]


def test_punctuation_cjk_and_accents_split_or_kept():
    wp = WordPiece(VOCAB)
    assert wp.encode("Ivan-Petrov")[0] == [4, 5, 13, 6, 7]
    assert wp.encode("日本")[0] == [11, 12]
    assert wp.encode("café")[0] == [14, 15]  # no accent stripping (cased)


def test_unknown_word_and_long_word_are_one_unk():
    wp = WordPiece(VOCAB, max_chars=10)
    assert wp.encode("Ivanx hi") == ([1, 10], [(0, 5), (6, 8)])
    assert wp.encode("I" * 11) == ([1], [(0, 11)])


def test_control_chars_dropped_whitespace_splits():
    wp = WordPiece(VOCAB)
    ids, offs = wp.encode("Iv\x00an hi​")
    assert ids == [4, 5, 10]
    assert offs == [(0, 2), (3, 5), (6, 8)]  # offsets point into the original text
    assert wp.encode("") == ([], [])
