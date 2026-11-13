"""Unit tests for deterministic result sanity checks."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from text2sql.execution import QueryResult
from text2sql.generation import SQLGeneration
from text2sql.guardrails.sql_guard import referenced_tables
from text2sql.schema import ColumnInfo, DatabaseSchema, TableInfo
from text2sql.verification.sanity import run_sanity_checks

SCHEMA = DatabaseSchema(tables=[
    TableInfo(name="orders", columns=[ColumnInfo(name=c, type="TEXT", nullable=True)
                                      for c in ("order_id", "status", "order_date")]),
    TableInfo(name="customers", columns=[ColumnInfo(name=c, type="TEXT", nullable=True)
                                         for c in ("customer_id", "birth_date")]),
])  # fmt: skip
SQL = "SELECT o.status, COUNT(*) AS n FROM orders o GROUP BY o.status"


def _result(columns: list[str], rows: list[list[Any]], truncated: bool = False) -> QueryResult:
    return QueryResult(columns=columns, rows=rows, row_count=len(rows), truncated=truncated,
                       duration_ms=1, estimated_cost=1, plan={})  # fmt: skip


def _gen(tables: list[str] | None = None, columns: list[str] | None = None) -> SQLGeneration:
    return SQLGeneration(kind="sql", explanation="e", sql=SQL, confidence=0.9,
                         tables_used=tables if tables is not None else ["orders"],
                         columns_used=columns or ["orders.status"],
                         clarification=None, refusal_reason=None)  # fmt: skip


def _checks(question: str, result: QueryResult, sql: str = SQL, gen=None) -> set[str]:
    findings = run_sanity_checks(question, sql, result, gen or _gen(), SCHEMA)
    return {f.check for f in findings}


def test_clean_result_has_no_findings() -> None:
    assert _checks("orders per status", _result(["status", "n"], [["completed", 10]])) == set()


def test_empty_and_truncated() -> None:
    assert "empty_result" in _checks("orders", _result(["n"], []))
    assert "truncated" in _checks("orders", _result(["n"], [[1]], truncated=True))


def test_all_null_column() -> None:
    assert "null_column" in _checks("x", _result(["status", "rev"], [["a", None], ["b", None]]))


def test_negative_revenue_is_an_error() -> None:
    findings = run_sanity_checks("revenue", SQL, _result(["net_revenue"], [[Decimal("-5.00")]]),
                                 _gen(), SCHEMA)  # fmt: skip
    assert [(f.check, f.severity) for f in findings] == [("negative_value", "error")]


def test_negative_allowed_for_other_columns() -> None:
    assert _checks("growth", _result(["growth_rate"], [[-0.12]])) == set()


def test_dates_outside_data_period() -> None:
    assert "date_after_data" in _checks("x", _result(["order_date"], [[date(2027, 1, 1)]]))
    assert "date_before_data" in _checks("x", _result(["month"], [[datetime(2019, 5, 1)]]))
    # A birth date before the period is normal.
    assert _checks("x", _result(["birth_date"], [[date(1980, 1, 1)]])) == set()
    # Orders placed late December ship in early January.
    assert _checks("x", _result(["shipped_date"], [[date(2026, 1, 4)]])) == set()


def test_top_n_mismatch() -> None:
    rows = [[str(i), i] for i in range(10)]
    assert "top_n_mismatch" in _checks("Top 5 stores by revenue", _result(["s", "n"], rows))
    assert "top_n_mismatch" not in _checks("Top 5 stores", _result(["s", "n"], rows[:5]))
    assert "top_n_mismatch" not in _checks("Top 5 stores", _result(["s", "n"], rows[:3]))


def test_model_contradicting_itself_about_tables() -> None:
    result = _result(["status", "n"], [["completed", 1]])
    assert "declared_tables_mismatch" in _checks("x", result, gen=_gen(tables=["customers"]))


def test_declared_unknown_column() -> None:
    result = _result(["status", "n"], [["completed", 1]])
    found = _checks("x", result, gen=_gen(columns=["orders.total_amount"]))
    assert "unknown_declared_columns" in found


def test_referenced_tables_ignores_ctes_and_functions() -> None:
    sql = ("WITH t AS (SELECT customer_id FROM orders) SELECT * FROM t "
           "JOIN customers c USING (customer_id), generate_series(1, 2) g")  # fmt: skip
    assert referenced_tables(sql) == {"orders", "customers"}
    assert referenced_tables("not sql at all (") == set()
