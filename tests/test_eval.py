"""Benchmark tooling: dataset integrity, result comparison, metrics, runner (no LLM, no DB)."""

from datetime import date, datetime
from decimal import Decimal
from importlib import resources
from typing import Any

import pytest
import yaml

from text2sql.eval.compare import results_match
from text2sql.eval.dataset import EvalCase, load_cases
from text2sql.eval.metrics import auc, summarize
from text2sql.eval.runner import CaseResult, InjectedResult, run_case
from text2sql.execution import QueryResult
from text2sql.generation import SQLGeneration, SQLGenerator
from text2sql.guardrails import GuardrailConfig, check_sql
from text2sql.llm import FakeLLMClient
from text2sql.pipeline import Text2SQLPipeline
from text2sql.prompt.ambiguity import load_glossary
from text2sql.prompt.builder import PromptBuilder
from text2sql.schema import ColumnInfo, DatabaseSchema, TableInfo

TABLES = frozenset(
    {"categories", "products", "stores", "customers", "orders", "order_items", "inventory"}
)
CASES = load_cases()


# ------------------------------------------------------------------ dataset
def test_dataset_size_and_mix() -> None:
    assert len(CASES) >= 50
    expects = {c.expect for c in CASES}
    assert expects == {"answer", "clarification", "no_answer", "unsafe"}
    assert sum(c.lang == "fr" for c in CASES) >= 10
    assert sum(c.bad_sql is not None for c in CASES) >= 20


def test_few_shot_questions_are_not_reused() -> None:
    raw = resources.files("text2sql.prompt").joinpath("examples.yaml").read_text("utf-8")
    examples = {e["question"].strip().lower() for e in yaml.safe_load(raw)}
    assert not examples & {c.question.strip().lower() for c in CASES}


@pytest.mark.parametrize("case", [c for c in CASES if c.expect == "answer"], ids=lambda c: c.id)
def test_reference_sql_passes_guardrails(case: EvalCase) -> None:
    for sql in [*case.golds, *([case.bad_sql] if case.bad_sql else [])]:
        result = check_sql(sql, GuardrailConfig(allowed_tables=TABLES))
        assert result.allowed, result.violations


def test_case_validation() -> None:
    with pytest.raises(ValueError, match="gold_sql"):
        EvalCase(id="x", category="c", lang="en", expect="answer", question="q")
    with pytest.raises(ValueError, match="bad_kind"):
        EvalCase(
            id="x",
            category="c",
            lang="en",
            expect="answer",
            question="q",
            gold_sql="SELECT 1",
            bad_sql="SELECT 2",
        )


# ---------------------------------------------------------------- compare
def test_match_ignores_row_and_column_order_and_extra_columns() -> None:
    gold = [["Books", Decimal("10.00")], ["Toys", Decimal("5.50")]]
    assert results_match(gold, [[5.5, "toys", 7], [10.0, "Books ", 3]])
    assert not results_match(gold, [[5.5, 7], [10.0, 3]])  # label missing


def test_numbers_tolerate_rounding_but_integers_are_exact() -> None:
    assert results_match([[Decimal("25.34")]], [[25.3]])
    assert not results_match([[Decimal("25.34")]], [[25.0]])
    assert not results_match([[2356]], [[2357]])
    assert results_match([[Decimal("3")]], [[3]])  # EXTRACT returns numeric
    assert not results_match([[Decimal("1695204.93")]], [[1690000.0]])


def test_dates_and_row_counts() -> None:
    assert results_match([[date(2025, 1, 1)]], [[datetime(2025, 1, 1)]])
    assert not results_match([[1], [2]], [[1]])
    assert results_match([[1], [1], [2]], [[2], [1], [1]])
    assert not results_match([[1], [1], [2]], [[2], [2], [1]])


# ---------------------------------------------------------------- metrics
def test_auc() -> None:
    assert auc([0.1, 0.2], [0.8, 0.9]) == 1.0
    assert auc([0.5], [0.5]) == 0.5
    assert auc([], [0.5]) is None


def _case(
    id_: str,
    expect: str,
    status: str,
    passed: bool,
    flagged: bool | None = None,
    score: float | None = None,
) -> CaseResult:
    return CaseResult(
        id=id_,
        category="c",
        lang="en",
        expect=expect,
        question="q",
        status=status,
        passed=passed,
        confidence=score,
        likely_hallucination=flagged,
        input_tokens=1000,
        output_tokens=100,
        llm_calls=4,
        duration_ms=100,
    )


def test_summarize() -> None:
    cases = [
        _case("a1", "answer", "executed", True, False, 0.9),
        _case("a2", "answer", "executed", False, True, 0.3),
        _case("n1", "no_answer", "executed", False, False, 0.7),  # made-up answer, missed
        _case("u1", "unsafe", "blocked", True),
    ]
    injected = [
        InjectedResult(
            id="a1",
            kind="wrong_year",
            label="bad",
            confidence=0.2,
            likely_hallucination=True,
            components={},
            reasons=[],
            input_tokens=10,
            output_tokens=1,
        )
    ]
    s = summarize(cases, injected, cost=lambda i, o: (i + o) / 1e6, writes_delta=0)
    assert s["execution_accuracy"]["rate"] == 0.5
    assert s["hallucination"]["all"]["bad_flagged"] == {"passed": 2, "total": 3, "rate": 0.6667}
    assert s["hallucination"]["natural"]["good_false_alarm"]["passed"] == 0
    assert s["unsafe_refused_or_blocked"]["rate"] == 1.0
    assert s["cost"]["per_question_usd"] == pytest.approx(0.0011)


# ----------------------------------------------------------------- runner
SCHEMA = DatabaseSchema(
    tables=[
        TableInfo(name="orders", columns=[ColumnInfo(name="status", type="TEXT", nullable=False)])
    ]
)


class RowsExecutor:
    """Returns 42 for the gold query, 7 for anything else."""

    def execute(self, sql: str) -> QueryResult:
        value = 42 if "gold" in sql else 7
        return QueryResult(
            columns=["n"],
            rows=[[value]],
            row_count=1,
            truncated=False,
            duration_ms=1,
            estimated_cost=1,
            plan={},
        )


def _pipeline(sql: str) -> Text2SQLPipeline:
    gen = SQLGeneration(
        kind="sql",
        explanation="e",
        sql=sql,
        confidence=0.9,
        tables_used=["orders"],
        columns_used=[],
        clarification=None,
        refusal_reason=None,
    )

    def responder(system: str, user: str, model: type) -> Any:
        return gen

    return Text2SQLPipeline(
        generator=SQLGenerator(PromptBuilder(SCHEMA, examples=[]), FakeLLMClient(responder)),
        guard_config=GuardrailConfig(allowed_tables=frozenset({"orders"})),
        executor=RowsExecutor(),
        glossary=load_glossary(),
    )


GOLD = "SELECT COUNT(*) AS gold FROM orders"


def test_run_case_answer() -> None:
    case = EvalCase(
        id="x", category="c", lang="en", expect="answer", question="How many?", gold_sql=GOLD
    )
    assert run_case(_pipeline(GOLD), case).passed
    wrong = run_case(_pipeline("SELECT COUNT(*) AS n FROM orders"), case)
    assert not wrong.passed and wrong.detail == "result differs from the reference"


def test_run_case_unsafe_and_clarification() -> None:
    unsafe = EvalCase(id="m", category="unsafe", lang="en", expect="unsafe", question="drop it")
    assert run_case(_pipeline("DROP TABLE orders"), unsafe).status == "blocked"
    assert run_case(_pipeline("DROP TABLE orders"), unsafe).passed
    assert not run_case(_pipeline(GOLD), unsafe).passed  # executed: counts as a failure
    clar = EvalCase(
        id="a", category="ambiguous", lang="en", expect="clarification", question="Revenue in 2025?"
    )
    assert run_case(_pipeline(GOLD), clar).passed  # glossary asks before the LLM
