"""Tests for engine/embedder.py — semantic embedding generation."""

import pytest

from screenmind.engine.embedder import Embedder


@pytest.fixture(scope="module")
def embedder():
    """Shared embedder instance (model load is expensive)."""
    e = Embedder()
    try:
        e._ensure_model()
    except OSError as exc:
        pytest.skip(f"Embedder model unavailable (offline?): {exc}")
    except ImportError as exc:
        pytest.skip(f"onnxruntime/tokenizers not installed: {exc}")
    return e


def test_embed_text_dimensions(embedder):
    vec = embedder.embed_text("Hello world")
    assert len(vec) == 384
    assert all(isinstance(v, float) for v in vec)


def test_embed_text_normalized(embedder):
    import numpy as np
    vec = np.array(embedder.embed_text("test sentence"))
    norm = np.linalg.norm(vec)
    assert abs(norm - 1.0) < 0.01  # Should be unit vector


def test_similar_texts_high_score(embedder):
    v1 = embedder.embed_text("writing Python code in VS Code")
    v2 = embedder.embed_text("coding in Python with Visual Studio Code")
    import numpy as np
    similarity = np.dot(v1, v2)
    assert similarity > 0.7  # Should be very similar


def test_different_texts_low_score(embedder):
    v1 = embedder.embed_text("writing Python code")
    v2 = embedder.embed_text("cooking pasta for dinner")
    import numpy as np
    similarity = np.dot(v1, v2)
    assert similarity < 0.4  # Should be dissimilar


def test_embed_activity(embedder):
    vec = embedder.embed_activity(
        summary="Editing auth middleware",
        details="Working on JWT token validation",
        app_name="VS Code",
        category="coding",
    )
    assert len(vec) == 384


def test_search(embedder):
    texts = [
        "debugging Python authentication code",
        "watching YouTube videos about cats",
        "writing unit tests for the API",
        "browsing Reddit memes",
    ]
    embeddings = [embedder.embed_text(t) for t in texts]

    results = embedder.search("fixing auth bugs in Python", embeddings, top_k=2)
    assert len(results) >= 1
    # First result should be the auth-related text
    assert results[0][0] == 0  # Index of "debugging Python authentication code"
    assert results[0][1] > 0.5  # High relevance
