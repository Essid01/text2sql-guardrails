"""End-to-end pipeline: question -> clarification | refusal | blocked | error | result.

question
  -> glossary ambiguity check   (free, deterministic)    -> clarification
  -> LLM generation             (structured output)      -> clarification | refusal
  -> guardrails                 (AST analysis, LIMIT)    -> blocked
  -> executor                   (read-only role, EXPLAIN) -> blocked | error | executed
"""

from __future__ import annotations

import logging
import time
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel

from text2sql.audit import AuditLog
from text2sql.execution import (
    ExecutionError,
    QueryResult,
    QueryTimeoutError,
    QueryTooExpensiveError,
)
from text2sql.generation import SQLGeneration, SQLGenerator
from text2sql.guardrails import GuardrailConfig, Violation, check_sql
from text2sql.llm.client import LLMError, LLMRefusalError, UsageTracker
from text2sql.prompt.ambiguity import (
    Clarification,
    ClarificationOption,
    GlossaryEntry,
    detect_ambiguities,
)
from text2sql.verification.verifier import VerificationReport, Verifier

logger = logging.getLogger(__name__)


class Status(StrEnum):
    EXECUTED = "executed"
    CLARIFICATION = "clarification"
    REFUSED = "refused"
    BLOCKED = "blocked"
    ERROR = "error"


class Executor(Protocol):
    def execute(self, sql: str) -> QueryResult: ...


class PipelineResponse(BaseModel):
    """Everything the API/UI needs to display, and the audit log needs to record."""

    question: str
    status: Status
    clarifications: list[Clarification] = []
    refusal_reason: str | None = None
    generation: SQLGeneration | None = None
    sql_executed: str | None = None  # SQL actually run (rewritten by the guardrails)
    violations: list[Violation] = []
    result: QueryResult | None = None
    verification: VerificationReport | None = None  # only for executed queries
    error: str | None = None
    usage: dict[str, float] = {}
    duration_ms: float = 0.0


class Text2SQLPipeline:
    """Orchestrates the stages; each dependency is injected (fakes in tests)."""

    def __init__(
        self,
        generator: SQLGenerator,
        guard_config: GuardrailConfig,
        executor: Executor,
        glossary: list[GlossaryEntry],
        audit: AuditLog | None = None,
        verifier: Verifier | None = None,
    ) -> None:
        self.generator = generator
        self.guard_config = guard_config
        self.executor = executor
        self.glossary = glossary
        self.audit = audit
        self.verifier = verifier

    def ask(self, question: str) -> PipelineResponse:
        start = time.perf_counter()
        tracker = UsageTracker()
        response = self._run(question.strip(), tracker)
        response.usage = tracker.total
        response.duration_ms = round((time.perf_counter() - start) * 1000, 1)
        self._audit(response)
        logger.info("question answered status=%s duration_ms=%.0f cost_usd=%s",
                    response.status, response.duration_ms, response.usage["cost_usd"])  # fmt: skip
        return response

    def _run(self, question: str, tracker: UsageTracker) -> PipelineResponse:
        # 1. Known ambiguities: ask the user before spending anything.
        if clarifications := detect_ambiguities(question, self.glossary):
            return PipelineResponse(
                question=question, status=Status.CLARIFICATION, clarifications=clarifications
            )

        # 2. Generation.
        try:
            generated = self.generator.generate(question)
        except LLMRefusalError as exc:
            return PipelineResponse(question=question, status=Status.REFUSED,
                                    refusal_reason=str(exc))  # fmt: skip
        except LLMError as exc:
            return PipelineResponse(question=question, status=Status.ERROR, error=str(exc))
        tracker.add(generated.usage)
        output = generated.output

        if output.kind == "clarification" and output.clarification is not None:
            clarification = Clarification(
                term_id="llm",
                question=output.clarification.question,
                options=[ClarificationOption(label=o, interpretation=o)
                         for o in output.clarification.options],
            )  # fmt: skip
            return PipelineResponse(question=question, status=Status.CLARIFICATION,
                                    clarifications=[clarification], generation=output)  # fmt: skip
        if output.kind == "refusal":
            return PipelineResponse(
                question=question,
                status=Status.REFUSED,
                refusal_reason=output.refusal_reason,
                generation=output,
            )

        # 3. Guardrails (the LLM output is untrusted input).
        guard = check_sql(output.sql or "", self.guard_config)
        if not guard.allowed or guard.sql is None:
            return PipelineResponse(question=question, status=Status.BLOCKED,
                                    generation=output, violations=guard.violations)  # fmt: skip

        # 4. Execution (read-only role, cost and time limits).
        try:
            result = self.executor.execute(guard.sql)
        except QueryTooExpensiveError as exc:
            return PipelineResponse(
                question=question, status=Status.BLOCKED, generation=output,
                sql_executed=None, violations=[Violation(rule="too_expensive", message=str(exc))],
            )  # fmt: skip
        except (QueryTimeoutError, ExecutionError) as exc:
            return PipelineResponse(question=question, status=Status.ERROR, generation=output,
                                    sql_executed=guard.sql, error=str(exc))  # fmt: skip

        # 5. Hallucination checks (only a valid, executed query can be verified).
        verification = None
        if self.verifier is not None:
            verification = self.verifier.verify(question, output, guard.sql, result)
            for usage in verification.usages:
                tracker.add(usage)
        return PipelineResponse(
            question=question,
            status=Status.EXECUTED,
            generation=output,
            sql_executed=guard.sql,
            result=result,
            verification=verification,
        )

    def _audit(self, r: PipelineResponse) -> None:
        if self.audit is None:
            return
        self.audit.write({
            "question": r.question,
            "status": r.status,
            "sql_generated": r.generation.sql if r.generation else None,
            "sql_executed": r.sql_executed,
            "llm_confidence": r.generation.confidence if r.generation else None,
            "violations": [v.model_dump() for v in r.violations],
            "clarification_terms": [c.term_id for c in r.clarifications],
            "refusal_reason": r.refusal_reason,
            "error": r.error,
            "row_count": r.result.row_count if r.result else None,
            "estimated_cost": r.result.estimated_cost if r.result else None,
            "confidence": r.verification.confidence.score if r.verification else None,
            "likely_hallucination": (
                r.verification.confidence.likely_hallucination if r.verification else None
            ),
            "agreement": (
                r.verification.consensus.agreement
                if r.verification and r.verification.consensus
                else None
            ),
            "duration_ms": r.duration_ms,
            "usage": r.usage,
        })  # fmt: skip
