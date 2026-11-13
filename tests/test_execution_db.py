"""Integration tests for the query executor (read-only role, test database)."""

import pytest

from text2sql.execution import (
    ExecutionError,
    QueryExecutor,
    QueryTimeoutError,
    QueryTooExpensiveError,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def executor(readonly_url: str) -> QueryExecutor:
    return QueryExecutor(readonly_url, max_rows=10, timeout_ms=2000, max_cost=100_000)


def test_returns_rows_columns_and_plan(executor: QueryExecutor) -> None:
    result = executor.execute(
        "SELECT o.status, COUNT(*) AS n FROM orders o GROUP BY o.status ORDER BY o.status"
    )
    assert result.columns == ["status", "n"]
    assert [r[0] for r in result.rows] == ["cancelled", "completed", "pending", "returned"]
    assert result.truncated is False and result.row_count == 4
    assert result.estimated_cost > 0
    assert "Node Type" in result.plan


def test_truncation_is_detected(executor: QueryExecutor) -> None:
    result = executor.execute("SELECT c.customer_id FROM customers c")
    assert result.row_count == 10 and result.truncated is True


def test_expensive_query_is_refused_before_running(executor: QueryExecutor) -> None:
    with pytest.raises(QueryTooExpensiveError, match="estimated cost"):
        executor.execute("SELECT COUNT(*) FROM orders a, order_items b, customers c")


def test_timeout_cancels_long_query(readonly_url: str) -> None:
    slow = QueryExecutor(readonly_url, timeout_ms=200, max_cost=float("inf"))
    with pytest.raises(QueryTimeoutError):
        slow.execute("SELECT COUNT(*) FROM generate_series(1, 100000000)")


def test_hallucinated_column_gives_clear_error(executor: QueryExecutor) -> None:
    with pytest.raises(ExecutionError, match="total_amount"):
        executor.execute("SELECT o.total_amount FROM orders o")


def test_write_is_refused_even_if_guardrails_were_bypassed(executor: QueryExecutor) -> None:
    with pytest.raises(ExecutionError):
        executor.execute("DELETE FROM orders")
