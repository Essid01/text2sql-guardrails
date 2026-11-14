"""Query history and user feedback, stored in the `app` schema (admin connection).

The read-only role used for generated SQL cannot read these tables (see app_schema.sql).
`InMemoryHistoryStore` has the same behaviour, for unit tests and API tests without a DB.
"""

from __future__ import annotations

import itertools
import logging
import threading
from datetime import UTC, datetime
from importlib import resources
from typing import Any, Literal, Protocol

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from text2sql.db.engine import conninfo
from text2sql.pipeline import PipelineResponse

logger = logging.getLogger(__name__)

Verdict = Literal["correct", "incorrect"]
STORED_ROWS = 100  # rows kept in the stored response: enough to review an answer


class FeedbackRequest(BaseModel):
    verdict: Verdict
    comment: str | None = Field(default=None, max_length=2000)
    corrected_sql: str | None = Field(
        default=None, max_length=10_000, description="What the right query would have been."
    )


class Feedback(FeedbackRequest):
    created_at: datetime


class HistoryItem(BaseModel):
    """One line of the history list."""

    query_id: int
    created_at: datetime
    question: str
    status: str
    sql_source: Literal["llm", "user"]
    sql_executed: str | None
    confidence: float | None
    likely_hallucination: bool | None
    row_count: int | None
    duration_ms: float
    cost_usd: float
    feedback: Feedback | None = None


class HistoryEntry(HistoryItem):
    """Full record: what was generated and the stored API response."""

    sql_generated: str | None
    response: dict[str, Any]


class HistoryStore(Protocol):
    def record(self, response: PipelineResponse) -> int: ...
    def list(self, limit: int = 50, offset: int = 0) -> list[HistoryItem]: ...
    def get(self, query_id: int) -> HistoryEntry | None: ...
    def set_feedback(self, query_id: int, feedback: FeedbackRequest) -> Feedback | None: ...
    def incorrect(self) -> list[HistoryEntry]: ...


def _columns(response: PipelineResponse) -> dict[str, Any]:
    """Flat columns + the JSON response with rows capped at STORED_ROWS."""
    stored = response.model_dump(mode="json")
    if stored.get("result"):
        stored["result"]["rows"] = stored["result"]["rows"][:STORED_ROWS]
        stored["result"]["plan"] = {}  # large and rarely useful afterwards
    confidence = response.verification.confidence if response.verification else None
    return {
        "question": response.question,
        "status": str(response.status),
        "sql_source": response.sql_source,
        "sql_generated": response.generation.sql if response.generation else None,
        "sql_executed": response.sql_executed,
        "confidence": confidence.score if confidence else None,
        "likely_hallucination": confidence.likely_hallucination if confidence else None,
        "row_count": response.result.row_count if response.result else None,
        "duration_ms": response.duration_ms,
        "cost_usd": round(response.usage.get("cost_usd", 0.0), 6),
        "response": stored,
    }


def ensure_app_schema(admin_url: str) -> None:
    """Create the `app` schema and tables if missing (idempotent, keeps data)."""
    ddl = resources.files("text2sql.db").joinpath("app_schema.sql").read_text("utf-8")
    with psycopg.connect(conninfo(admin_url), autocommit=True) as conn:
        conn.execute(ddl)  # type: ignore[arg-type]  # trusted static file
    logger.info("app schema ready")


_SELECT = """
SELECT h.query_id, h.created_at, h.question, h.status, h.sql_source, h.sql_generated,
       h.sql_executed, h.confidence, h.likely_hallucination, h.row_count, h.duration_ms,
       h.cost_usd::float8 AS cost_usd, {response}
       f.verdict, f.comment, f.corrected_sql, f.created_at AS feedback_at
FROM app.query_history h LEFT JOIN app.feedback f USING (query_id)
"""


class PostgresHistoryStore:
    """History in PostgreSQL. One short connection per call (low traffic, simple)."""

    def __init__(self, admin_url: str) -> None:
        self._conninfo = conninfo(admin_url)

    def _connect(self) -> psycopg.Connection[dict[str, Any]]:
        return psycopg.connect(self._conninfo, row_factory=dict_row)

    def record(self, response: PipelineResponse) -> int:
        cols = _columns(response)
        cols["response"] = Jsonb(cols["response"])
        names = ", ".join(cols)
        placeholders = ", ".join(f"%({k})s" for k in cols)
        query = (
            f"INSERT INTO app.query_history ({names}) VALUES ({placeholders}) RETURNING query_id"  # noqa: S608  # column names are constants
        )
        with self._connect() as conn:
            row = conn.execute(query, cols).fetchone()  # type: ignore[arg-type]
        assert row is not None  # noqa: S101  # RETURNING always yields a row
        return int(row["query_id"])

    def list(self, limit: int = 50, offset: int = 0) -> list[HistoryItem]:
        query = _SELECT.format(response="") + " ORDER BY h.query_id DESC LIMIT %s OFFSET %s"
        with self._connect() as conn:
            rows = conn.execute(query, (limit, offset)).fetchall()  # type: ignore[arg-type]
        return [_parse(row, HistoryItem) for row in rows]

    def get(self, query_id: int) -> HistoryEntry | None:
        query = _SELECT.format(response="h.response,") + " WHERE h.query_id = %s"
        with self._connect() as conn:
            row = conn.execute(query, (query_id,)).fetchone()  # type: ignore[arg-type]
        return _parse(row, HistoryEntry) if row else None

    def set_feedback(self, query_id: int, feedback: FeedbackRequest) -> Feedback | None:
        query = """
            INSERT INTO app.feedback (query_id, verdict, comment, corrected_sql)
            SELECT query_id, %(verdict)s, %(comment)s, %(corrected_sql)s
            FROM app.query_history WHERE query_id = %(query_id)s
            ON CONFLICT (query_id) DO UPDATE SET verdict = EXCLUDED.verdict,
                comment = EXCLUDED.comment, corrected_sql = EXCLUDED.corrected_sql,
                created_at = now()
            RETURNING created_at
        """
        params = {"query_id": query_id, **feedback.model_dump()}
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()  # type: ignore[arg-type]
        return Feedback(**feedback.model_dump(), created_at=row["created_at"]) if row else None

    def incorrect(self) -> list[HistoryEntry]:
        query = _SELECT.format(response="h.response,") + (
            " WHERE f.verdict = 'incorrect' ORDER BY h.query_id"
        )
        with self._connect() as conn:
            rows = conn.execute(query).fetchall()  # type: ignore[arg-type]
        return [_parse(row, HistoryEntry) for row in rows]


def _parse(row: dict[str, Any], model: type[HistoryItem]) -> Any:
    verdict = row.pop("verdict")
    feedback_fields = {k: row.pop(k) for k in ("comment", "corrected_sql", "feedback_at")}
    feedback = None
    if verdict is not None:
        feedback = Feedback(verdict=verdict, comment=feedback_fields["comment"],
                            corrected_sql=feedback_fields["corrected_sql"],
                            created_at=feedback_fields["feedback_at"])  # fmt: skip
    return model.model_validate({**row, "feedback": feedback})


class InMemoryHistoryStore:
    """Same contract as PostgresHistoryStore, kept in a dict (tests, demos without DB)."""

    def __init__(self) -> None:
        self._entries: dict[int, HistoryEntry] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def record(self, response: PipelineResponse) -> int:
        with self._lock:
            query_id = next(self._ids)
            self._entries[query_id] = HistoryEntry(
                query_id=query_id, created_at=datetime.now(UTC), **_columns(response)
            )
        return query_id

    def list(self, limit: int = 50, offset: int = 0) -> list[HistoryItem]:
        newest_first = sorted(self._entries.values(), key=lambda e: -e.query_id)
        return [HistoryItem.model_validate(e.model_dump()) for e in newest_first][
            offset : offset + limit
        ]

    def get(self, query_id: int) -> HistoryEntry | None:
        return self._entries.get(query_id)

    def set_feedback(self, query_id: int, feedback: FeedbackRequest) -> Feedback | None:
        entry = self._entries.get(query_id)
        if entry is None:
            return None
        entry.feedback = Feedback(**feedback.model_dump(), created_at=datetime.now(UTC))
        return entry.feedback

    def incorrect(self) -> list[HistoryEntry]:
        return [
            e for e in sorted(self._entries.values(), key=lambda e: e.query_id)
            if e.feedback and e.feedback.verdict == "incorrect"
        ]  # fmt: skip
