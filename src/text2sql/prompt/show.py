"""Print the prompt that would be sent for a question (no LLM call, no cost).

Usage:
    python -m text2sql.prompt.show "What was the net revenue per category in 2025?"
"""

from __future__ import annotations

import argparse
import logging

from text2sql.config import get_settings
from text2sql.db.engine import get_engine
from text2sql.prompt.ambiguity import detect_ambiguities, load_glossary
from text2sql.prompt.builder import PromptBuilder
from text2sql.schema import introspect
from text2sql.schema.embedder import Embedder, SentenceTransformerEmbedder
from text2sql.schema.retriever import SchemaRetriever


def _embedder(model_name: str) -> Embedder | None:
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        return None
    return SentenceTransformerEmbedder(model_name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Show the SQL-generation prompt.")
    parser.add_argument("question")
    args = parser.parse_args()

    settings = get_settings()
    logging.basicConfig(level="WARNING")

    clarifications = detect_ambiguities(args.question, load_glossary())
    if clarifications:
        print("=== CLARIFICATION NEEDED (no LLM call would be made) ===")
        for c in clarifications:
            options = " | ".join(o.label for o in c.options)
            print(f"- {c.question}  [{options}]")
        print()

    schema = introspect(get_engine(settings.database_url))
    embedder = _embedder(settings.embedding_model)
    retriever = None
    if settings.schema_top_k is not None and embedder is not None:
        retriever = SchemaRetriever(schema, embedder, top_k=settings.schema_top_k)
    prompt = PromptBuilder(schema, embedder=embedder, retriever=retriever).build(args.question)

    print("=== SYSTEM ===")
    print(prompt.system)
    print("\n=== USER ===")
    print(prompt.user)
    size = len(prompt.system) + len(prompt.user)
    print(f"\n=== tables={len(prompt.tables)} examples={prompt.example_ids} ", end="")
    print(f"size={size} chars (~{size // 4} tokens) ===")


if __name__ == "__main__":
    main()
