"""Assemble the SQL-generation prompt: static instructions + schema + examples + question."""

from __future__ import annotations

import logging
from importlib import resources

import numpy as np
import yaml
from pydantic import BaseModel

from text2sql.prompt.schema_render import render_schema
from text2sql.schema.embedder import Embedder
from text2sql.schema.models import DatabaseSchema
from text2sql.schema.retriever import SchemaRetriever

logger = logging.getLogger(__name__)


class FewShotExample(BaseModel):
    """A hand-written question with its reference SQL."""

    id: str
    question: str
    sql: str


class BuiltPrompt(BaseModel):
    """What is sent to the LLM, plus metadata for logs and evaluation."""

    system: str  # static: identical for every question (cache-friendly)
    user: str  # variable: schema, examples, question
    tables: list[str]  # tables included in the schema section
    example_ids: list[str]  # few-shot examples included, most similar first


def load_system_prompt() -> str:
    """The SQL-generation instructions (templates/sql_system.md)."""
    text = resources.files("text2sql.prompt").joinpath("templates/sql_system.md")
    return text.read_text("utf-8").strip()


def load_examples() -> list[FewShotExample]:
    """The few-shot pool (examples.yaml)."""
    raw = resources.files("text2sql.prompt").joinpath("examples.yaml").read_text("utf-8")
    return [FewShotExample.model_validate(item) for item in yaml.safe_load(raw)]


def _format_examples(examples: list[FewShotExample]) -> str:
    blocks = [
        f"<example>\nQuestion: {ex.question}\nSQL:\n{ex.sql.strip()}\n</example>" for ex in examples
    ]
    return "<examples>\n" + "\n".join(blocks) + "\n</examples>"


class PromptBuilder:
    """Builds prompts for one database schema.

    - Few-shot examples: the `n_examples` most similar to the question when an
      embedder is given, otherwise the first ones of the pool (deterministic).
    - Schema: full schema, or only the retrieved tables when a retriever is given.
    """

    def __init__(
        self,
        schema: DatabaseSchema,
        examples: list[FewShotExample] | None = None,
        embedder: Embedder | None = None,
        n_examples: int = 3,
        retriever: SchemaRetriever | None = None,
        system_suffix: str = "",
    ) -> None:
        self.schema = schema
        self.system = load_system_prompt() + system_suffix
        self.examples = load_examples() if examples is None else examples
        self.embedder = embedder
        self.n_examples = n_examples
        self.retriever = retriever
        self._full_schema_text = render_schema(schema)
        self._example_vectors: np.ndarray | None = None
        if embedder is not None and self.examples:
            self._example_vectors = embedder.embed([ex.question for ex in self.examples])

    def select_examples(self, question: str) -> list[FewShotExample]:
        """Most similar examples first; ties broken by pool order."""
        if self._example_vectors is None or self.embedder is None:
            return self.examples[: self.n_examples]
        scores = self._example_vectors @ self.embedder.embed([question])[0]
        order = sorted(range(len(self.examples)), key=lambda i: (-scores[i], i))
        return [self.examples[i] for i in order[: self.n_examples]]

    def build(self, question: str) -> BuiltPrompt:
        question = question.strip()
        if not question:
            raise ValueError("question must not be empty")

        if self.retriever is None:
            tables, schema_text = self.schema.table_names, self._full_schema_text
        else:
            tables = self.retriever.retrieve(question).tables
            schema_text = render_schema(self.schema.subset(set(tables)))

        examples = self.select_examples(question)
        sections = [f"<schema>\n{schema_text}\n</schema>"]
        if examples:
            sections.append(_format_examples(examples))
        sections.append(f"<question>\n{question}\n</question>")

        prompt = BuiltPrompt(
            system=self.system,
            user="\n\n".join(sections),
            tables=tables,
            example_ids=[ex.id for ex in examples],
        )
        logger.info("prompt built tables=%d examples=%s", len(tables), prompt.example_ids)
        return prompt
