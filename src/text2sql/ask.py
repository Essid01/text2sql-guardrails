"""Command line: ask a question, get SQL + result.

Usage:
    python -m text2sql.ask "Top 3 categories by net revenue in 2025"
"""

from __future__ import annotations

import argparse
import logging

from text2sql.audit import AuditLog
from text2sql.config import Settings, get_settings
from text2sql.db.engine import get_engine
from text2sql.execution import QueryExecutor
from text2sql.generation import SQLGenerator
from text2sql.guardrails import GuardrailConfig
from text2sql.llm.factory import build_llm_client
from text2sql.pipeline import PipelineResponse, Status, Text2SQLPipeline
from text2sql.prompt.ambiguity import load_glossary
from text2sql.prompt.builder import PromptBuilder
from text2sql.schema import introspect
from text2sql.schema.embedder import Embedder, SentenceTransformerEmbedder
from text2sql.schema.retriever import SchemaRetriever
from text2sql.verification.backtranslation import BackTranslator
from text2sql.verification.consensus import ALTERNATE_SUFFIX, ConsensusChecker
from text2sql.verification.verifier import Verifier


def _optional_embedder(settings: Settings) -> Embedder | None:
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        return None
    return SentenceTransformerEmbedder(settings.embedding_model)


def build_pipeline(settings: Settings, use_embedder: bool = True) -> Text2SQLPipeline:
    """Wire every component from the settings (.env)."""
    if not settings.readonly_database_url:
        raise RuntimeError("READONLY_DATABASE_URL is not set: generated SQL must not run as admin")
    schema = introspect(get_engine(settings.database_url))
    embedder = _optional_embedder(settings) if use_embedder else None
    retriever = None
    if settings.schema_top_k is not None and embedder is not None:
        retriever = SchemaRetriever(schema, embedder, top_k=settings.schema_top_k)
    builder = PromptBuilder(schema, embedder=embedder, retriever=retriever)
    llm = build_llm_client(settings)
    guard_config = GuardrailConfig(
        allowed_tables=frozenset(schema.table_names), max_rows=settings.max_result_rows
    )
    executor = QueryExecutor(
        settings.readonly_database_url,
        max_rows=settings.max_result_rows,
        timeout_ms=settings.query_timeout_ms,
        max_cost=settings.max_query_cost,
    )
    consensus = None
    if settings.verify_multi_query:
        # Zero-shot + extra instruction: a different prompt and a different approach.
        alternate = PromptBuilder(schema, examples=[], system_suffix=ALTERNATE_SUFFIX)
        consensus = ConsensusChecker(SQLGenerator(alternate, llm), guard_config, executor)
    verifier = Verifier(
        schema,
        backtranslator=BackTranslator(llm, schema) if settings.verify_backtranslation else None,
        consensus=consensus,
        threshold=settings.hallucination_threshold,
    )
    return Text2SQLPipeline(
        generator=SQLGenerator(builder, llm),
        guard_config=guard_config,
        executor=executor,
        glossary=load_glossary(),
        audit=AuditLog(settings.audit_log_path),
        verifier=verifier,
    )


def render(response: PipelineResponse, max_rows: int = 20) -> str:
    """Human-readable summary of a pipeline response."""
    lines = [f"status: {response.status}"]
    gen = response.generation
    if gen is not None:
        lines.append(f"explanation: {gen.explanation}")
        lines.append(f"llm confidence: {gen.confidence:.2f}")
    if response.status == Status.CLARIFICATION:
        for c in response.clarifications:
            lines.append(f"? {c.question}")
            lines += [f"   - {o.label}" for o in c.options]
    if response.refusal_reason:
        lines.append(f"refused: {response.refusal_reason}")
    if response.violations:
        if gen is not None and gen.sql:
            lines.append(f"generated sql: {' '.join(gen.sql.split())}")
        lines += [f"blocked [{v.rule}]: {v.message}" for v in response.violations]
    if response.sql_executed:
        lines.append(f"sql: {response.sql_executed}")
    if response.error:
        lines.append(f"error: {response.error}")
    if (result := response.result) is not None:
        lines.append(" | ".join(result.columns))
        lines += [" | ".join(str(v) for v in row) for row in result.rows[:max_rows]]
        more = " (truncated)" if result.truncated else ""
        lines.append(f"({result.row_count} rows{more}, {result.duration_ms} ms, "
                     f"cost {result.estimated_cost:,.0f})")  # fmt: skip
    if (v := response.verification) is not None:
        c = v.confidence
        flag = "  <-- LIKELY HALLUCINATION" if c.likely_hallucination else ""
        lines.append(f"confidence: {c.score:.2f} ({c.level}){flag}")
        parts = ", ".join(
            f"{k}={'n/a' if x is None else f'{x:.2f}'}" for k, x in c.components.items()
        )
        lines.append(f"  signals: {parts}")
        if v.backtranslation:
            lines.append(f"  back-translation: {v.backtranslation.reconstructed_question}")
        if v.consensus:
            lines.append(f"  second query: {v.consensus.agreement} ({v.consensus.detail})")
        lines += [f"  ! {reason}" for reason in c.reasons]
    u = response.usage
    lines.append(f"llm: {u.get('api_calls', 0):.0f} call(s), {u.get('input_tokens', 0):.0f} in / "
                 f"{u.get('output_tokens', 0):.0f} out tokens, ${u.get('cost_usd', 0):.5f} "
                 f"| total {response.duration_ms:.0f} ms")  # fmt: skip
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ask a question to the retail database.")
    parser.add_argument("question")
    parser.add_argument("--no-embedder", action="store_true",
                        help="skip loading the embedding model (faster start)")  # fmt: skip
    args = parser.parse_args()
    settings = get_settings()
    logging.basicConfig(level="WARNING")
    pipeline = build_pipeline(settings, use_embedder=not args.no_embedder)
    print(render(pipeline.ask(args.question)))


if __name__ == "__main__":
    main()
