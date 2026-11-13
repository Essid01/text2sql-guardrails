"""Deterministic sanity checks on a result (free: no LLM call).

They cannot prove a query right, but they catch typical symptoms of a wrong one:
a broken join (all-NULL column), an impossible value (negative revenue), a date the
data cannot contain, a forgotten LIMIT, or a model contradicting itself about the
tables it used.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from text2sql.execution import QueryResult
from text2sql.generation import SQLGeneration
from text2sql.guardrails.sql_guard import referenced_tables
from text2sql.schema.models import DatabaseSchema

Severity = Literal["info", "warning", "error"]

# Column names whose values can never be negative.
NON_NEGATIVE = re.compile(
    r"revenue|amount|sales|total|quantity|units|count|price|spend|orders|customers|stock"
)
# Column names that hold order-period dates (other dates, e.g. birth_date, may be older).
PERIOD_DATE = re.compile(r"order|month|week|day|period|^date$")
TOP_N = re.compile(r"\b(?:top|first|best|worst|biggest|largest)\s+(\d{1,3})\b", re.IGNORECASE)


class Finding(BaseModel):
    """One suspicious observation about a result."""

    check: str
    severity: Severity
    message: str


def run_sanity_checks(
    question: str,
    sql: str,
    result: QueryResult,
    generation: SQLGeneration,
    schema: DatabaseSchema,
    period: tuple[date, date] = (date(2024, 1, 1), date(2025, 12, 31)),
) -> list[Finding]:
    """All findings for one executed query (empty list = nothing suspicious)."""
    findings: list[Finding] = []
    findings += _check_shape(question, result)
    findings += _check_columns(result, period)
    findings += _check_declarations(sql, generation, schema)
    return findings


def _check_shape(question: str, result: QueryResult) -> list[Finding]:
    out: list[Finding] = []
    if result.row_count == 0:
        out.append(Finding(check="empty_result", severity="warning",
                           message="the query returned no rows"))  # fmt: skip
    if result.truncated:
        message = f"only the first {result.row_count} rows are shown"
        out.append(Finding(check="truncated", severity="info", message=message))
    if (m := TOP_N.search(question)) and result.row_count > int(m.group(1)):
        message = f"asked for {m.group(1)} rows, got {result.row_count}"
        out.append(Finding(check="top_n_mismatch", severity="warning", message=message))
    return out


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else None


def _check_columns(result: QueryResult, period: tuple[date, date]) -> list[Finding]:
    out: list[Finding] = []
    if not result.rows:
        return out
    start, end = period
    for i, name in enumerate(result.columns):
        values = [row[i] for row in result.rows]
        lowered = name.lower()
        if all(v is None for v in values):
            out.append(Finding(check="null_column", severity="warning",
                               message=f"column '{name}' is entirely NULL (broken join or "
                                       "filter matching nothing?)"))  # fmt: skip
            continue
        numbers = [v for v in values if isinstance(v, int | float | Decimal)
                   and not isinstance(v, bool)]  # fmt: skip
        if NON_NEGATIVE.search(lowered) and any(v < 0 for v in numbers):
            out.append(Finding(check="negative_value", severity="error",
                               message=f"column '{name}' has negative values"))  # fmt: skip
        dates = [d for v in values if (d := _as_date(v)) is not None]
        # Small margin after the period: late orders ship in early January.
        if any(d > end + timedelta(days=31) for d in dates):
            out.append(Finding(check="date_after_data", severity="warning",
                               message=f"column '{name}' has dates after {end}"))  # fmt: skip
        if PERIOD_DATE.search(lowered) and any(d < start for d in dates):
            out.append(Finding(check="date_before_data", severity="warning",
                               message=f"column '{name}' has dates before {start}"))  # fmt: skip
    return out


def _check_declarations(
    sql: str, generation: SQLGeneration, schema: DatabaseSchema
) -> list[Finding]:
    """The model's description of its own query must match the query."""
    out: list[Finding] = []
    actual = referenced_tables(sql)
    declared = {t.lower() for t in generation.tables_used}
    if declared != actual:
        out.append(Finding(
            check="declared_tables_mismatch", severity="warning",
            message=f"model declared {sorted(declared)} but the SQL uses {sorted(actual)}",
        ))  # fmt: skip
    known = {f"{t.name}.{c.name}" for t in schema.tables for c in t.columns}
    unknown = sorted(c for c in generation.columns_used if "." in c and c.lower() not in known)
    if unknown:
        message = f"model declared columns not in the schema: {unknown}"
        out.append(Finding(check="unknown_declared_columns", severity="warning", message=message))
    return out
