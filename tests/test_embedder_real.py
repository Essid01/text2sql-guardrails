"""Smoke test with the real local model (skipped when sentence-transformers is absent)."""

import numpy as np
import pytest

from text2sql.config import Settings
from text2sql.schema.embedder import SentenceTransformerEmbedder

pytestmark = pytest.mark.embeddings


@pytest.fixture(scope="module")
def embedder() -> SentenceTransformerEmbedder:
    # Imported here, not at module level: importing torch takes ~40 s and would slow
    # down the collection of every test run, even when these tests are deselected.
    pytest.importorskip("sentence_transformers")
    return SentenceTransformerEmbedder(Settings(_env_file=None).embedding_model)


def test_vectors_are_normalized(embedder: SentenceTransformerEmbedder) -> None:
    vectors = embedder.embed(["net revenue per category", "stock below reorder level"])
    assert vectors.shape[0] == 2
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5)


def test_semantic_similarity_beats_unrelated(embedder: SentenceTransformerEmbedder) -> None:
    q, related, unrelated = embedder.embed(
        ["customers in the gold loyalty tier", "Registered customers, loyalty tier", "Garden hose"]
    )
    assert q @ related > q @ unrelated
