"""Unit tests for multi-query validation (result comparison and checker)."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from text2sql.execution import ExecutionError, QueryResult
from text2sql.generation import SQLGeneration, SQLGenerator
from text2sql.guardrails import GuardrailConfig
from text2sql.llm import FakeLLMClient
from text2sql.prompt.builder import PromptBuilder
from text2sql.schema import ColumnInfo, DatabaseSchema, TableInfo
from text2sql.verification.consensus import (
    ALTERNATE_SUFFIX,
    Agreement,
    ConsensusChecker,
    compare_results,
)


def _r(columns: list[str], rows: list[list[Any]]) -> QueryResult:
    return QueryResult(columns=columns, rows=rows, row_count=len(rows), truncated=False,
                       duration_ms=1, estimated_cost=1, plan={})  # fmt: skip


def test_same_rows_in_any_order_match() -> None:
    a = _r(["type", "rev"], [["online", Decimal("10.00")], ["physical", Decimal("20.5")]])
    b = _r(["rev", "type"], [[20.499999, "Physical"], [10, "online"]])  # order, case, rounding
    assert compare_results(a, b)[0] == Agreement.MATCH


def test_same_numbers_with_different_labels_match() -> None:
    a = _r(["store_name", "n"], [["Lyon Store", 5], ["Paris Store", 7]])
    b = _r(["store_id", "n"], [[3, 5], [9, 7]])
    agreement, detail = compare_results(a, b)
    assert agreement == Agreement.MISMATCH  # ids are numbers too: no false match
    c = _r(["n"], [[5], [7]])
    d = _r(["label", "n"], [["x", 5], ["y", 7]])
    assert compare_results(c, d)[0] == Agreement.MATCH
    assert "different labels" in compare_results(c, d)[1]


def test_different_values_or_row_counts_mismatch() -> None:
    assert compare_results(_r(["n"], [[1]]), _r(["n"], [[2]]))[0] == Agreement.MISMATCH
    assert compare_results(_r(["n"], [[1]]), _r(["n"], [[1], [1]]))[0] == Agreement.MISMATCH


def test_dates_normalized() -> None:
    a = _r(["m"], [[date(2025, 1, 1)]])
    b = _r(["m"], [[datetime(2025, 1, 1, 0, 0)]])
    assert compare_results(a, b)[0] == Agreement.MATCH


SCHEMA = DatabaseSchema(tables=[TableInfo(name="orders", columns=[
    ColumnInfo(name="status", type="TEXT", nullable=False)])])  # fmt: skip
GUARD = GuardrailConfig(allowed_tables=frozenset({"orders"}))


def _gen(sql: str | None, kind: str = "sql") -> SQLGeneration:
    return SQLGeneration(kind=kind, explanation="e", sql=sql, confidence=0.9, tables_used=[],
                         columns_used=[], clarification=None,
                         refusal_reason="no" if kind == "refusal" else None)  # fmt: skip


class Exec:
    def __init__(self, result: Any) -> None:
        self.result = result

    def execute(self, sql: str) -> QueryResult:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _checker(output: SQLGeneration, result: Any) -> tuple[ConsensusChecker, FakeLLMClient]:
    fake = FakeLLMClient(lambda s, u, m: output)
    builder = PromptBuilder(SCHEMA, examples=[], system_suffix=ALTERNATE_SUFFIX)
    return ConsensusChecker(SQLGenerator(builder, fake), GUARD, Exec(result)), fake


def test_checker_match_and_independent_prompt() -> None:
    primary = _r(["n"], [[42]])
    checker, fake = _checker(_gen("SELECT COUNT(*) FROM orders"), _r(["count"], [[42]]))
    result = checker.check("How many orders?", primary)
    assert result.agreement == Agreement.MATCH
    assert "Independent solution" in fake.calls[0][0]  # different system prompt
    assert "<examples>" not in fake.calls[0][1]  # zero-shot


def test_checker_unavailable_cases() -> None:
    primary = _r(["n"], [[42]])
    refusal = _checker(_gen(None, kind="refusal"), primary)[0].check("q", primary)
    blocked = _checker(_gen("DELETE FROM orders"), primary)[0].check("q", primary)
    failed = _checker(_gen("SELECT x FROM orders"), ExecutionError("boom"))[0].check("q", primary)
    assert {refusal.agreement, blocked.agreement, failed.agreement} == {Agreement.UNAVAILABLE}
    assert "blocked" in blocked.detail and "failed" in failed.detail
