"""History and feedback stores: same contract in memory and in PostgreSQL."""

from datetime import date
from decimal import Decimal

import psycopg
import pytest

from text2sql.db.engine import conninfo
from text2sql.execution import QueryResult
from text2sql.history import (
    STORED_ROWS,
    FeedbackRequest,
    InMemoryHistoryStore,
    PostgresHistoryStore,
    ensure_app_schema,
)
from text2sql.pipeline import PipelineResponse, Status


def _response(question: str = "How many orders?", rows: int = 1) -> PipelineResponse:
    result = QueryResult(columns=["n", "day"], rows=[[Decimal("1.50"), date(2025, 1, 1)]] * rows,
                         row_count=rows, truncated=False, duration_ms=1.0, estimated_cost=5.0,
                         plan={"Node Type": "Seq Scan"})  # fmt: skip
    return PipelineResponse(question=question, status=Status.EXECUTED,
                            sql_executed="SELECT 1 LIMIT 1000", result=result,
                            usage={"cost_usd": 0.0012345678}, duration_ms=12.5)  # fmt: skip


@pytest.fixture
def pg_store(seeded_db: str):
    ensure_app_schema(seeded_db)
    ensure_app_schema(seeded_db)  # idempotent
    with psycopg.connect(conninfo(seeded_db), autocommit=True) as conn:
        conn.execute("TRUNCATE app.query_history CASCADE")
    return PostgresHistoryStore(seeded_db)


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.integration)])
def store(request):
    if request.param == "memory":
        return InMemoryHistoryStore()
    return request.getfixturevalue("pg_store")


def test_record_and_get(store) -> None:
    query_id = store.record(_response(rows=STORED_ROWS + 20))
    entry = store.get(query_id)
    assert entry is not None and entry.question == "How many orders?"
    assert entry.status == "executed" and entry.sql_source == "llm"
    assert entry.row_count == STORED_ROWS + 20
    assert entry.cost_usd == pytest.approx(0.001235)
    stored = entry.response["result"]
    assert len(stored["rows"]) == STORED_ROWS  # capped
    assert stored["rows"][0] == [1.5, "2025-01-01"]  # JSON types, numbers stay numbers
    assert stored["plan"] == {}
    assert store.get(query_id + 1000) is None


def test_list_newest_first_with_paging(store) -> None:
    ids = [store.record(_response(f"q{i}")) for i in range(3)]
    assert [i.query_id for i in store.list()] == ids[::-1]
    assert [i.query_id for i in store.list(limit=1, offset=1)] == [ids[1]]


def test_feedback_latest_verdict_wins(store) -> None:
    query_id = store.record(_response())
    assert store.list()[0].feedback is None
    store.set_feedback(query_id, FeedbackRequest(verdict="correct"))
    stored = store.set_feedback(query_id, FeedbackRequest(
        verdict="incorrect", comment="wrong year", corrected_sql="SELECT 2"))  # fmt: skip
    assert stored is not None and stored.created_at is not None
    item = store.list()[0]
    assert item.feedback is not None and item.feedback.verdict == "incorrect"
    assert [e.query_id for e in store.incorrect()] == [query_id]
    assert store.incorrect()[0].feedback.corrected_sql == "SELECT 2"


def test_feedback_on_unknown_query(store) -> None:
    assert store.set_feedback(999_999, FeedbackRequest(verdict="correct")) is None


def test_only_incorrect_answers_are_exported(store) -> None:
    good, bad = store.record(_response("good")), store.record(_response("bad"))
    store.set_feedback(good, FeedbackRequest(verdict="correct"))
    store.set_feedback(bad, FeedbackRequest(verdict="incorrect"))
    assert [e.question for e in store.incorrect()] == ["bad"]
