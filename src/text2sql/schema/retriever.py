"""Select the tables relevant to a question: embedding similarity + FK bridging."""

from __future__ import annotations

import logging
from collections import deque
from itertools import combinations

from pydantic import BaseModel

from text2sql.schema.embedder import Embedder
from text2sql.schema.models import DatabaseSchema, TableInfo

logger = logging.getLogger(__name__)


class RetrievalResult(BaseModel):
    """Tables kept for the prompt, and why."""

    tables: list[str]  # final selection, in schema order
    scores: dict[str, float]  # cosine similarity of every table to the question
    by_similarity: list[str]  # top-k tables, best first
    added_as_bridge: list[str]  # tables added only to make the selection joinable


def table_document(table: TableInfo) -> str:
    """Text that represents a table for embedding: names, comments, categorical values."""
    parts = [f"Table {table.name.replace('_', ' ')}."]
    if table.comment:
        parts.append(table.comment)
    for col in table.columns:
        desc = col.name.replace("_", " ")
        if col.comment:
            desc += f" ({col.comment})"
        if col.sample_values and len(col.sample_values) <= 10:
            desc += f" values: {', '.join(col.sample_values)}"
        parts.append(desc + ".")
    return " ".join(parts)


def shortest_fk_path(schema: DatabaseSchema, start: str, goal: str) -> list[str] | None:
    """Shortest chain of tables linking `start` to `goal` through FKs (BFS), or None."""
    previous: dict[str, str | None] = {start: None}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        if current == goal:
            path = [current]
            while (prev := previous[path[-1]]) is not None:
                path.append(prev)
            return path[::-1]
        for nxt in sorted(schema.neighbors(current)):  # sorted: deterministic tie-breaking
            if nxt not in previous:
                previous[nxt] = current
                queue.append(nxt)
    return None


class SchemaRetriever:
    """Keeps the `top_k` most similar tables, then adds bridge tables needed for JOINs.

    Why bridging: "revenue per category" is similar to `categories` and `order_items`,
    but the JOIN also needs `products`, which may not score high on its own.
    """

    def __init__(self, schema: DatabaseSchema, embedder: Embedder, top_k: int | None = 4) -> None:
        self.schema = schema
        self.embedder = embedder
        self.top_k = top_k
        # Table vectors are computed once; only the question is embedded per call.
        self._table_vectors = embedder.embed([table_document(t) for t in schema.tables])

    def retrieve(self, question: str) -> RetrievalResult:
        names = self.schema.table_names
        similarities = self._table_vectors @ self.embedder.embed([question])[0]
        scores = {name: round(float(s), 4) for name, s in zip(names, similarities, strict=True)}
        ranked = sorted(names, key=lambda n: (-scores[n], n))

        top = ranked if self.top_k is None else ranked[: self.top_k]

        selected = set(top)
        for a, b in combinations(top, 2):
            path = shortest_fk_path(self.schema, a, b)
            if path:
                selected.update(path)
        bridges = [n for n in names if n in selected and n not in top]

        result = RetrievalResult(
            tables=[n for n in names if n in selected],
            scores=scores,
            by_similarity=top,
            added_as_bridge=bridges,
        )
        logger.info("schema retrieval top=%s bridges=%s", top, bridges)
        return result
