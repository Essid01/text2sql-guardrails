"""Execute guardrail-approved SQL with the read-only role, under cost and time limits."""

from __future__ import annotations

import logging
import time
from typing import Any

import psycopg
from psycopg import errors
from pydantic import BaseModel

from text2sql.db.engine import conninfo

logger = logging.getLogger(__name__)


class ExecutionError(Exception):
    """The database rejected or could not run the query."""


class QueryTooExpensiveError(ExecutionError):
    """The planner's cost estimate is above the configured maximum."""


class QueryTimeoutError(ExecutionError):
    """The query was cancelled by the statement timeout."""


class QueryResult(BaseModel):
    """Rows and execution metadata."""

    columns: list[str]
    rows: list[list[Any]]
    row_count: int  # rows returned (after truncation)
    truncated: bool  # True if the query produced more than max_rows rows
    duration_ms: float
    estimated_cost: float  # planner total cost (EXPLAIN), arbitrary units
    plan: dict[str, Any]  # top node of EXPLAIN (FORMAT JSON)


class QueryExecutor:
    """Runs one query per read-only transaction.

    Order of operations, each one a safety net:
    1. read-only transaction + local statement timeout (on top of the role defaults);
    2. EXPLAIN first: queries whose estimated cost exceeds `max_cost` never run;
    3. fetch at most `max_rows + 1` rows to detect truncation without loading everything;
    4. always roll back.

    Measured on the demo data: normal analytical queries cost 300-4,000 and run in
    < 70 ms; cartesian products and large self-joins cost > 700,000 and time out.
    """

    def __init__(
        self,
        readonly_url: str,
        max_rows: int = 1000,
        timeout_ms: int = 5000,
        max_cost: float = 100_000,
    ) -> None:
        self._conninfo = conninfo(readonly_url)
        self.max_rows = max_rows
        self.timeout_ms = timeout_ms
        self.max_cost = max_cost

    def execute(self, sql: str) -> QueryResult:
        """Run `sql`, which must already have passed the guardrails."""
        start = time.perf_counter()
        try:
            with psycopg.connect(self._conninfo) as conn:
                conn.read_only = True
                with conn.transaction(force_rollback=True):
                    conn.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        (str(self.timeout_ms),),
                    )
                    # String concatenation is deliberate: `sql` is the guardrail output (one
                    # SELECT regenerated from a checked AST), it cannot be parameterized,
                    # and it runs in a read-only transaction under a read-only role.
                    (plan_json,) = conn.execute(f"EXPLAIN (FORMAT JSON) {sql}").fetchone()
                    plan = plan_json[0]["Plan"]
                    cost = float(plan["Total Cost"])
                    if cost > self.max_cost:
                        raise QueryTooExpensiveError(
                            f"estimated cost {cost:,.0f} exceeds the limit {self.max_cost:,.0f}"
                        )
                    cursor = conn.execute(sql)
                    columns = [col.name for col in cursor.description or []]
                    fetched = cursor.fetchmany(self.max_rows + 1)
        except errors.QueryCanceled as exc:
            raise QueryTimeoutError(f"query exceeded {self.timeout_ms} ms") from exc
        except psycopg.Error as exc:
            # e.g. undefined column/table from a hallucinated query: a useful signal.
            message = exc.diag.message_primary if exc.diag else str(exc)
            raise ExecutionError(f"{type(exc).__name__}: {message}") from exc

        truncated = len(fetched) > self.max_rows
        rows = [list(r) for r in fetched[: self.max_rows]]
        duration = round((time.perf_counter() - start) * 1000, 1)
        logger.info("query executed rows=%d truncated=%s cost=%.0f duration_ms=%.1f",
                    len(rows), truncated, cost, duration)  # fmt: skip
        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            duration_ms=duration,
            estimated_cost=cost,
            plan=plan,
        )
