"""Text embedders behind a minimal interface, so the backend can be swapped."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from functools import cached_property
from typing import Protocol

import numpy as np


class Embedder(Protocol):
    """Turns texts into L2-normalized vectors (dot product = cosine similarity)."""

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """Return an array of shape (len(texts), dim), each row L2-normalized."""
        ...


class SentenceTransformerEmbedder:
    """Local open-source model (no API key, runs on CPU)."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name

    @cached_property
    def _model(self):  # noqa: ANN202 - optional dependency, imported lazily
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - depends on install
            raise RuntimeError(
                'sentence-transformers is not installed: pip install -e ".[embeddings]"'
            ) from exc
        return SentenceTransformer(self.model_name, device="cpu")

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        return self._model.encode(
            list(texts), normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False
        )


class HashingEmbedder:
    """Deterministic bag-of-words embedder for tests: similarity = shared words.

    No model download, no randomness: tests and CI stay fast and reproducible.
    """

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for token in re.findall(r"[a-z0-9]+", text.lower()):
                token = token.removesuffix("s")  # crude plural folding: orders ~ order
                bucket = int(hashlib.md5(token.encode(), usedforsecurity=False).hexdigest(), 16)
                out[i, bucket % self.dim] += 1.0
            norm = np.linalg.norm(out[i])
            if norm > 0:
                out[i] /= norm
        return out
