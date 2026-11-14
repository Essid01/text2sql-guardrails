"""Unit tests for the end-to-end pipeline (fake LLM, fake executor, no database)."""

import json
from typing import Any

import pytest

from text2sql.audit import AuditLog
from text2sql.execution import ExecutionError, QueryResult, QueryTooExpensiveError
from text2sql.generation import ClarificationRequest, SQLGeneration, SQLGenerator
from text2sql.guardrails import GuardrailConfig
from text2sql.llm import FakeLLMClient, LLMRefusalError
from text2sql.pipeline import Status, Text2SQLPipeline
from text2sql.prompt.ambiguity import load_glossary
from text2sql.prompt.builder import PromptBuilder
from text2sql.schema import ColumnInfo, DatabaseSchema, TableInfo

SCHEMA = DatabaseSchema(
    tables=[TableInfo(name="orders", columns=[ColumnInfo(name="status", type="TEXT",
                                                         nullable=False)])]
)  # fmt: skip
GUARD = GuardrailConfig(allowed_tables=frozenset({"orders"}), max_rows=100)


def _sql(sql: str) -> SQLGeneration:
    return SQLGeneration(kind="sql", explanation="e", sql=sql, confidence=0.9,
                         tables_used=["orders"], columns_used=[], clarification=None,
                         refusal_reason=None)  # fmt: skip


class FakeExecutor:
    def __init__(self, outcome: Any = None) -> None:
        self.outcome = outcome
        self.executed: list[str] = []

    def execute(self, sql: str) -> QueryResult:
        self.executed.append(sql)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return QueryResult(columns=["n"], rows=[[42]], row_count=1, truncated=False,
                           duration_ms=1.0, estimated_cost=10.0, plan={})  # fmt: skip


def _pipeline(output: Any, executor: FakeExecutor | None = None, audit: AuditLog | None = None):
    def responder(system: str, user: str, model: type) -> SQLGeneration:
        if isinstance(output, Exception):
            raise output
        return output

    fake_llm = FakeLLMClient(responder)
    pipeline = Text2SQLPipeline(
        generator=SQLGenerator(PromptBuilder(SCHEMA, examples=[]), fake_llm),
        guard_config=GUARD,
        executor=executor or FakeExecutor(),
        glossary=load_glossary(),
        audit=audit,
    )
    return pipeline, fake_llm


def test_success_runs_guarded_sql() -> None:
    executor = FakeExecutor()
    pipeline, _ = _pipeline(_sql("SELECT COUNT(*) AS n FROM orders"), executor)
    response = pipeline.ask("How many orders?")
    assert response.status == Status.EXECUTED
    assert response.result.rows == [[42]]
    assert executor.executed == ["SELECT COUNT(*) AS n FROM orders LIMIT 100"]  # LIMIT added
    assert response.sql_executed == executor.executed[0]


def test_glossary_ambiguity_short_circuits_llm() -> None:
    pipeline, fake_llm = _pipeline(_sql("SELECT 1"))
    response = pipeline.ask("What is the revenue per store?")
    assert response.status == Status.CLARIFICATION
    assert response.clarifications[0].term_id == "revenue_gross_or_net"
    assert fake_llm.calls == []  # no LLM call, no cost
    assert response.usage["cost_usd"] == 0


def test_llm_clarification_is_passed_through() -> None:
    request = ClarificationRequest(question="Which metric?", options=["units", "orders"])
    output = SQLGeneration(kind="clarification", explanation="e", sql=None, confidence=0.3,
                           tables_used=[], columns_used=[], refusal_reason=None,
                           clarification=request)  # fmt: skip
    response = _pipeline(output)[0].ask("Which store performs best?")
    assert response.status == Status.CLARIFICATION
    assert [o.label for o in response.clarifications[0].options] == ["units", "orders"]


def test_llm_refusal_output() -> None:
    output = SQLGeneration(kind="refusal", explanation="e", sql=None, confidence=1.0,
                           tables_used=[], columns_used=[], clarification=None,
                           refusal_reason="read-only")  # fmt: skip
    response = _pipeline(output)[0].ask("Delete all orders")
    assert response.status == Status.REFUSED and response.refusal_reason == "read-only"


def test_model_refusal_error_becomes_refused() -> None:
    response = _pipeline(LLMRefusalError("declined"))[0].ask("something")
    assert response.status == Status.REFUSED


def test_dangerous_sql_is_blocked_and_never_executed() -> None:
    executor = FakeExecutor()
    pipeline, _ = _pipeline(_sql("SELECT 1; DROP TABLE orders"), executor)
    response = pipeline.ask("How many orders?")
    assert response.status == Status.BLOCKED
    assert response.violations[0].rule == "multiple_statements"
    assert executor.executed == []


def test_too_expensive_is_blocked() -> None:
    executor = FakeExecutor(QueryTooExpensiveError("estimated cost 9,999,999"))
    response = _pipeline(_sql("SELECT * FROM orders"), executor)[0].ask("All orders")
    assert response.status == Status.BLOCKED
    assert response.violations[0].rule == "too_expensive"


def test_execution_error_is_reported() -> None:
    executor = FakeExecutor(ExecutionError("UndefinedColumn: column o.total does not exist"))
    response = _pipeline(_sql("SELECT total FROM orders"), executor)[0].ask("Totals")
    assert response.status == Status.ERROR
    assert "does not exist" in response.error


@pytest.mark.parametrize("question", ["How many orders?", "What is the revenue per store?"])
def test_every_question_is_audited(tmp_path, question: str) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    _pipeline(_sql("SELECT COUNT(*) AS n FROM orders"), audit=audit)[0].ask(question)
    records = [json.loads(line) for line in audit.path.read_text("utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["question"] == question
    assert records[0]["status"] in {"executed", "clarification"}
    assert "timestamp" in records[0] and "usage" in records[0]


def test_executed_answer_is_verified_and_audited(tmp_path) -> None:
    from text2sql.verification.verifier import Verifier

    audit = AuditLog(tmp_path / "audit.jsonl")
    pipeline, _ = _pipeline(_sql("SELECT COUNT(*) AS n FROM orders"), audit=audit)
    pipeline.verifier = Verifier(SCHEMA)
    response = pipeline.ask("How many orders?")
    assert response.verification is not None
    assert 0 <= response.verification.confidence.score <= 1
    record = json.loads(audit.path.read_text("utf-8").splitlines()[0])
    assert record["confidence"] == response.verification.confidence.score
    assert record["likely_hallucination"] is False


@pytest.mark.parametrize("sql", ["DROP TABLE orders", "DELETE FROM orders",
                                 "SELECT * FROM app.query_history"])  # fmt: skip
def test_user_sql_goes_through_guardrails(sql: str) -> None:
    executor = FakeExecutor()
    pipeline, fake_llm = _pipeline(_sql("SELECT 1"), executor)
    response = pipeline.run_user_sql("my question", sql)
    assert response.status == Status.BLOCKED and response.sql_source == "user"
    assert executor.executed == [] and fake_llm.calls == []


def test_user_sql_is_executed_without_llm_call() -> None:
    executor = FakeExecutor()
    pipeline, fake_llm = _pipeline(_sql("SELECT 1"), executor)
    response = pipeline.run_user_sql("How many orders?", "SELECT COUNT(*) AS n FROM orders")
    assert response.status == Status.EXECUTED and response.sql_source == "user"
    assert response.result is not None and response.verification is None
    assert executor.executed == ["SELECT COUNT(*) AS n FROM orders LIMIT 100"]
    assert fake_llm.calls == [] and response.usage["cost_usd"] == 0
