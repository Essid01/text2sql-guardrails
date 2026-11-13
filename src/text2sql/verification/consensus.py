"""Multi-query validation: a second, independent generation; compare RESULTS, not SQL.

Two different queries that return the same rows are strong evidence; disagreement is
a strong hallucination signal. Independence matters: the second prompt is zero-shot
(no few-shot examples) with an extra instruction, so it is a different prompt (no cache
hit returning the same answer) and a different approach.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from text2sql.execution import ExecutionError, QueryResult
from text2sql.generation import SQLGenerator
from text2sql.guardrails import GuardrailConfig, check_sql
from text2sql.llm.client import CallUsage, LLMError

ALTERNATE_SUFFIX = """

# Independent solution

Another analyst already wrote a query for this question. Solve it on your own from the
schema, without trying to guess their query; a different but equivalent formulation is
welcome."""


class Agreement(StrEnum):
    MATCH = "match"  # same rows
    MISMATCH = "mismatch"  # different rows
    UNAVAILABLE = "unavailable"  # second query not produced, blocked or failed


def _normalize(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int | float | Decimal):
        number = float(value)
        return None if math.isnan(number) else round(number, 2)
    if isinstance(value, datetime):
        return (
            value.date().isoformat() if value.time() == datetime.min.time() else value.isoformat()
        )
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip().lower()


def _row_key(row: list[Any]) -> tuple[str, ...]:
    """Row as an order-independent tuple: column order and names are ignored."""
    return tuple(sorted(repr(_normalize(v)) for v in row))


def _numeric_key(row: list[Any]) -> tuple[float, ...]:
    return tuple(
        sorted(
            _normalize(v)
            for v in row
            if isinstance(v, int | float | Decimal) and not isinstance(v, bool)
        )
    )


def compare_results(a: QueryResult, b: QueryResult) -> tuple[Agreement, str]:
    """Compare two results as multisets of rows (order of rows/columns ignored).

    Exact match on normalized values first; otherwise accept a numeric match (same
    numbers per row) because two correct queries often differ only by a label
    column (e.g. one query also returns the category name). Known limit: a name in one
    query and a numeric id in the other is reported as a mismatch.
    """
    if a.row_count != b.row_count:
        return Agreement.MISMATCH, f"{a.row_count} rows vs {b.row_count} rows"
    if Counter(map(_row_key, a.rows)) == Counter(map(_row_key, b.rows)):
        return Agreement.MATCH, "identical rows"
    numeric_a, numeric_b = Counter(map(_numeric_key, a.rows)), Counter(map(_numeric_key, b.rows))
    if numeric_a == numeric_b and any(any(k) for k in numeric_a):
        return Agreement.MATCH, "same numbers, different labels or columns"
    return Agreement.MISMATCH, "different values"


class ConsensusResult(BaseModel):
    agreement: Agreement
    detail: str
    alternate_sql: str | None = None
    usages: list[CallUsage] = []


class ConsensusChecker:
    """Generates an alternate query, runs it under the same guardrails, compares results."""

    def __init__(self, generator: SQLGenerator, guard_config: GuardrailConfig, executor: Any):
        self.generator = generator
        self.guard_config = guard_config
        self.executor = executor

    def check(self, question: str, primary: QueryResult) -> ConsensusResult:
        try:
            generated = self.generator.generate(question)
        except LLMError as exc:
            return ConsensusResult(agreement=Agreement.UNAVAILABLE, detail=f"llm error: {exc}")
        usages = [generated.usage]
        output = generated.output
        if output.kind != "sql" or not output.sql:
            return ConsensusResult(
                agreement=Agreement.UNAVAILABLE,
                usages=usages,
                detail=f"alternate answer was a {output.kind}",
            )
        guard = check_sql(output.sql, self.guard_config)
        if not guard.allowed or guard.sql is None:
            rules = ", ".join(v.rule for v in guard.violations)
            return ConsensusResult(
                agreement=Agreement.UNAVAILABLE,
                usages=usages,
                alternate_sql=output.sql,
                detail=f"alternate blocked: {rules}",
            )
        try:
            alternate = self.executor.execute(guard.sql)
        except ExecutionError as exc:
            return ConsensusResult(
                agreement=Agreement.UNAVAILABLE,
                usages=usages,
                alternate_sql=guard.sql,
                detail=f"alternate failed: {exc}",
            )
        agreement, detail = compare_results(primary, alternate)
        return ConsensusResult(
            agreement=agreement, detail=detail, alternate_sql=guard.sql, usages=usages
        )
