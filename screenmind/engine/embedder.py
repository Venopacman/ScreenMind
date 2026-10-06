"""
Embedding Engine
Generates semantic embeddings for activity summaries. Used for natural
language search over activities.

Runs the sentence-transformers all-MiniLM-L6-v2 model as ONNX on ONNX Runtime,
with the same tokenizer, 256-token limit, mean pooling and normalization, so
vectors match the ones sentence-transformers produced. No PyTorch needed.
"""

import logging
import threading
import numpy as np
from typing import List, Optional

logger = logging.getLogger("screenmind.engine.embedder")

# all-MiniLM-L6-v2 truncates input at 256 word pieces (its max_seq_length).
_MAX_TOKENS = 256
_MODEL_FILES = ("onnx/model.onnx", "tokenizer.json")


class Embedder:
    """
    Generates 384-dimensional embeddings using all-MiniLM-L6-v2.
    Tiny (~90MB), runs on CPU instantly, doesn't compete for GPU with Gemma.
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        self._model_name = model_name
        self._repo_id = model_name if "/" in model_name else f"sentence-transformers/{model_name}"
        self._model = None  # onnxruntime.InferenceSession
        self._tokenizer = None
        self._input_names: tuple = ()
        self._initialized = False
        self._lock = threading.Lock()

    def _ensure_model(self):
        """Lazy-load the embedding model on first use."""
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            try:
                import onnxruntime as ort
                from huggingface_hub import hf_hub_download
                from tokenizers import Tokenizer
            except ImportError:
                logger.warning("onnxruntime/tokenizers not installed. Semantic search disabled.")
                raise

            try:
                from screenmind.engine.model_manager import _models_dir
                local_dir = _models_dir() / "embedder" / self._model_name.split("/")[-1]
                if not all((local_dir / f).exists() for f in _MODEL_FILES):
                    logger.info(f"Downloading embedding model: {self._model_name} (~90MB, first time only)...")
                paths = {f: hf_hub_download(self._repo_id, f, local_dir=str(local_dir)) for f in _MODEL_FILES}

                tokenizer = Tokenizer.from_file(paths["tokenizer.json"])
                tokenizer.enable_truncation(max_length=_MAX_TOKENS)
                tokenizer.no_padding()

                opts = ort.SessionOptions()
                opts.log_severity_level = 3
                session = ort.InferenceSession(
                    paths["onnx/model.onnx"], sess_options=opts, providers=["CPUExecutionProvider"],
                )
                self._input_names = tuple(i.name for i in session.get_inputs())
                self._tokenizer = tokenizer
                self._model = session
                self._initialized = True
                logger.info(f"Model loaded. Dimensions: {self.dimensions}")
            except Exception as e:
                logger.error(f"Failed to load model: {e}")
                raise

    def _encode(self, text: str) -> np.ndarray:
        """Mean-pooled, L2-normalized sentence embedding (sentence-transformers' recipe)."""
        enc = self._tokenizer.encode(text)
        feeds = {
            "input_ids": np.array([enc.ids], dtype=np.int64),
            "attention_mask": np.array([enc.attention_mask], dtype=np.int64),
            "token_type_ids": np.array([enc.type_ids], dtype=np.int64),
        }
        feeds = {k: v for k, v in feeds.items() if k in self._input_names}
        tokens = self._model.run(None, feeds)[0][0]  # (seq_len, 384)
        mask = feeds["attention_mask"][0][:, None].astype(np.float32)
        vec = (tokens * mask).sum(axis=0) / max(float(mask.sum()), 1e-9)
        return vec / max(float(np.linalg.norm(vec)), 1e-12)

    def embed_text(self, text: str) -> List[float]:
        """
        Generate an embedding vector for a text string.

        Args:
            text: The text to embed (typically activity summary + details).

        Returns:
            List of 384 floats representing the semantic embedding.
        """
        self._ensure_model()
        return self._encode(text).astype(np.float32).tolist()

    def embed_activity(
        self,
        summary: str = "",
        details: str = "",
        visible_text: Optional[List[str]] = None,
        app_name: str = "",
        category: str = "",
        scene_description: str = "",
    ) -> List[float]:
        """
        Generate an embedding for an activity by combining multiple text fields.
        This produces better search results than embedding just the summary.

        Args:
            summary: Activity summary from Gemma.
            details: Detailed context from Gemma.
            visible_text: Text snippets visible on screen.
            app_name: Application name.
            category: Activity category.
            scene_description: Rich visual narration of the screenshot.

        Returns:
            384-dimensional embedding vector.
        """
        # Combine fields with decreasing importance
        parts = []
        if summary:
            parts.append(summary)
        if scene_description:
            # Truncate for embedding (MiniLM has 256 token limit)
            parts.append(scene_description[:500])
        if details:
            parts.append(details)
        if app_name:
            parts.append(f"Application: {app_name}")
        if category:
            parts.append(f"Category: {category}")
        if visible_text:
            parts.append("Visible: " + " | ".join(visible_text[:5]))

        combined = ". ".join(parts)
        return self.embed_text(combined)

    def search(
        self,
        query: str,
        embeddings: List[List[float]],
        top_k: int = 10,
    ) -> List[tuple]:
        """
        Find the most similar embeddings to a query.

        Args:
            query: Natural language search query.
            embeddings: List of stored embedding vectors.
            top_k: Number of top results to return.

        Returns:
            List of (index, similarity_score) tuples, sorted by relevance.
        """
        self._ensure_model()

        if not embeddings:
            return []

        query_embedding = np.array(self.embed_text(query))
        stored = np.array(embeddings)

        # Cosine similarity (embeddings are already normalized)
        similarities = stored @ query_embedding

        # Get top-k indices
        top_indices = np.argsort(similarities)[::-1][:top_k]

        return [
            (int(idx), float(similarities[idx]))
            for idx in top_indices
            if similarities[idx] > 0.1  # Min relevance threshold
        ]

    @property
    def dimensions(self) -> int:
        """Embedding dimensions (384 for all-MiniLM-L6-v2)."""
        return 384

    @property
    def is_available(self) -> bool:
        """Check if the embedding model can be loaded."""
        try:
            self._ensure_model()
            return True
        except Exception:
            return False
