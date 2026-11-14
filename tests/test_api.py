"""API tests: fake LLM, fake executor, in-memory history (no network, no database)."""

from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from text2sql.api.app import Service, create_app
from text2sql.execution import QueryResult
from text2sql.generation import SQLGeneration, SQLGenerator
from text2sql.guardrails import GuardrailConfig
from text2sql.history import InMemoryHistoryStore
from text2sql.llm import FakeLLMClient
from text2sql.pipeline import Text2SQLPipeline
from text2sql.prompt.ambiguity import load_glossary
from text2sql.prompt.builder import PromptBuilder
from text2sql.schema import ColumnInfo, DatabaseSchema, TableInfo

SCHEMA = DatabaseSchema(tables=[TableInfo(name="orders", columns=[
    ColumnInfo(name="status", type="TEXT", nullable=False)])])  # fmt: skip
GENERATION = SQLGeneration(kind="sql", explanation="Counts orders.",
                           sql="SELECT COUNT(*) AS n FROM orders", confidence=0.9,
                           tables_used=["orders"], columns_used=[], clarification=None,
                           refusal_reason=None)  # fmt: skip


class FakeExecutor:
    def __init__(self) -> None:
        self.executed: list[str] = []

    def execute(self, sql: str) -> QueryResult:
        self.executed.append(sql)
        return QueryResult(columns=["n"], rows=[[Decimal("42.50")]], row_count=1,
                           truncated=False, duration_ms=1.0, estimated_cost=10.0,
                           plan={})  # fmt: skip


@pytest.fixture
def ctx() -> dict[str, Any]:
    llm = FakeLLMClient(lambda system, user, model: GENERATION)
    executor = FakeExecutor()
    pipeline = Text2SQLPipeline(
        generator=SQLGenerator(PromptBuilder(SCHEMA, examples=[]), llm),
        guard_config=GuardrailConfig(allowed_tables=frozenset({"orders"}), max_rows=100),
        executor=executor,
        glossary=load_glossary(),
    )
    service = Service(pipeline=pipeline, history=InMemoryHistoryStore(), schema=SCHEMA)
    with TestClient(create_app(service)) as client:
        yield {"client": client, "llm": llm, "executor": executor}


def test_health(ctx) -> None:
    assert ctx["client"].get("/health").json() == {"status": "ok"}


def test_query_executes_and_is_recorded(ctx) -> None:
    client = ctx["client"]
    body = client.post("/v1/query", json={"question": "How many orders?"}).json()
    assert body["status"] == "executed" and body["query_id"] == 1
    assert body["sql_executed"] == "SELECT COUNT(*) AS n FROM orders LIMIT 100"
    assert body["result"]["rows"] == [[42.5]]  # Decimal serialized as a number
    history = client.get("/v1/history").json()
    assert [h["query_id"] for h in history] == [1]
    assert client.get("/v1/history/1").json()["sql_generated"] == GENERATION.sql


def test_clarification_costs_nothing(ctx) -> None:
    body = ctx["client"].post("/v1/query", json={"question": "Revenue in March"}).json()
    assert body["status"] == "clarification" and body["clarifications"]
    assert ctx["llm"].calls == []


def test_user_sql_is_guarded(ctx) -> None:
    client = ctx["client"]
    blocked = client.post("/v1/query", json={"question": "q", "sql": "DROP TABLE orders"}).json()
    assert blocked["status"] == "blocked" and blocked["sql_source"] == "user"
    assert ctx["executor"].executed == []
    ok = client.post("/v1/query", json={"question": "q", "sql": "SELECT status FROM orders"})
    assert ok.json()["status"] == "executed" and ctx["llm"].calls == []


@pytest.mark.parametrize("payload", [{}, {"question": ""}, {"question": "x" * 1001}])
def test_invalid_requests_are_rejected(ctx, payload: dict[str, Any]) -> None:
    assert ctx["client"].post("/v1/query", json=payload).status_code == 422


def test_feedback_roundtrip(ctx) -> None:
    client = ctx["client"]
    query_id = client.post("/v1/query", json={"question": "How many orders?"}).json()["query_id"]
    response = client.put(f"/v1/history/{query_id}/feedback",
                          json={"verdict": "incorrect", "comment": "wrong"})  # fmt: skip
    assert response.status_code == 200
    assert response.json()["feedback"]["verdict"] == "incorrect"
    assert client.get("/v1/history").json()[0]["feedback"]["comment"] == "wrong"


def test_feedback_errors(ctx) -> None:
    client = ctx["client"]
    assert client.put("/v1/history/99/feedback", json={"verdict": "correct"}).status_code == 404
    assert client.get("/v1/history/99").status_code == 404
    client.post("/v1/query", json={"question": "How many orders?"})
    assert client.put("/v1/history/1/feedback", json={"verdict": "maybe"}).status_code == 422


def test_history_limit_is_bounded(ctx) -> None:
    assert ctx["client"].get("/v1/history", params={"limit": 1000}).status_code == 422


def test_schema_endpoint(ctx) -> None:
    body = ctx["client"].get("/v1/schema").json()
    assert [t["name"] for t in body["tables"]] == ["orders"]


def test_openapi_lists_the_endpoints(ctx) -> None:
    paths = ctx["client"].get("/openapi.json").json()["paths"]
    assert {"/v1/query", "/v1/schema", "/v1/history",
            "/v1/history/{query_id}/feedback"} <= set(paths)  # fmt: skip
