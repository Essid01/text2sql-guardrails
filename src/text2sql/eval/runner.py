"""Run the evaluation: every case through the real pipeline, plus injected bad queries.

Two measurements of hallucination detection:
- natural: wrong answers the system actually produced on the benchmark (few, by luck);
- injected: for cases with a `bad_sql` (a typical LLM mistake written by hand), the
  verifier runs on the bad query AND on the gold query, under identical conditions
  (same question, same self-confidence). Detection rate on the bad ones, false-alarm
  rate on the gold ones.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

import psycopg
from pydantic import BaseModel

from text2sql.db.engine import conninfo
from text2sql.eval.compare import results_match
from text2sql.eval.dataset import EvalCase
from text2sql.execution import ExecutionError, QueryResult
from text2sql.generation import SQLGeneration
from text2sql.guardrails import check_sql
from text2sql.guardrails.sql_guard import referenced_tables
from text2sql.pipeline import Status, Text2SQLPipeline

logger = logging.getLogger(__name__)
INJECTED_SELF_CONFIDENCE = 0.9  # same value for bad and gold: the signal is neutral


class CaseResult(BaseModel):
    id: str
    category: str
    lang: str
    expect: str
    question: str
    status: str
    passed: bool
    sql_executed: str | None = None  # SQL run by the system
    gold_sql: str | None = None
    detail: str = ""
    confidence: float | None = None
    likely_hallucination: bool | None = None
    components: dict[str, float | None] = {}
    input_tokens: int = 0  # kept on cache hits: cost is computed from tokens
    output_tokens: int = 0
    llm_calls: int = 0
    duration_ms: float = 0.0


class InjectedResult(BaseModel):
    id: str
    kind: str  # bad_kind, or "gold"
    label: Literal["bad", "good"]
    confidence: float
    likely_hallucination: bool
    components: dict[str, float | None]
    reasons: list[str]
    input_tokens: int
    output_tokens: int


class ReferenceError(Exception):
    """A gold or bad query of the dataset does not run: fix the dataset."""


def _run_reference(pipeline: Text2SQLPipeline, sql: str) -> tuple[str, QueryResult]:
    guard = check_sql(sql, pipeline.guard_config)
    if not guard.allowed or guard.sql is None:
        raise ReferenceError(f"reference SQL rejected by guardrails: {guard.violations}")
    try:
        return guard.sql, pipeline.executor.execute(guard.sql)
    except ExecutionError as exc:
        raise ReferenceError(f"reference SQL failed: {exc}") from exc


def run_case(pipeline: Text2SQLPipeline, case: EvalCase) -> CaseResult:
    response = pipeline.ask(case.question)
    status = response.status
    verification = response.verification
    passed, detail = False, ""
    if case.expect == "answer":
        if status == Status.EXECUTED and response.result is not None:
            rows = response.result.rows
            for gold in case.golds:
                if results_match(_run_reference(pipeline, gold)[1].rows, rows):
                    passed, detail = True, "result matches the reference"
                    break
            else:
                detail = "result differs from the reference"
        else:
            detail = f"expected an executed answer, got {status}"
    elif case.expect == "clarification":
        passed = status == Status.CLARIFICATION
        detail = "" if passed else f"expected a clarification, got {status}"
    else:  # no_answer, unsafe: anything but an executed query
        passed = status != Status.EXECUTED
        detail = "" if passed else "a query was executed"
    conf = verification.confidence if verification else None
    return CaseResult(
        id=case.id,
        category=case.category,
        lang=case.lang,
        expect=case.expect,
        question=case.question,
        status=str(status),
        passed=passed,
        sql_executed=response.sql_executed,
        gold_sql=case.gold_sql,
        detail=detail or (response.error or ""),
        confidence=conf.score if conf else None,
        likely_hallucination=conf.likely_hallucination if conf else None,
        components=conf.components if conf else {},
        input_tokens=int(response.usage.get("input_tokens", 0)),
        output_tokens=int(response.usage.get("output_tokens", 0)),
        llm_calls=int(response.usage.get("llm_calls", 0)),
        duration_ms=response.duration_ms,
    )


def run_injected(pipeline: Text2SQLPipeline, case: EvalCase) -> list[InjectedResult]:
    """Verifier on the bad query and on the gold query of `case`."""
    assert case.bad_sql and case.gold_sql and case.bad_kind  # noqa: S101  # filtered by caller
    if pipeline.verifier is None:
        raise RuntimeError("injected evaluation needs the verifier")
    executed = {
        label: _run_reference(pipeline, sql)
        for label, sql in (("bad", case.bad_sql), ("good", case.gold_sql))
    }
    if results_match(executed["good"][1].rows, executed["bad"][1].rows):
        raise ReferenceError(f"{case.id}: bad_sql returns the same result as gold_sql")
    out = []
    for label, sql in (("bad", case.bad_sql), ("good", case.gold_sql)):
        generation = SQLGeneration(
            kind="sql",
            explanation="",
            sql=sql,
            confidence=INJECTED_SELF_CONFIDENCE,
            tables_used=sorted(referenced_tables(sql)),
            columns_used=[],
            clarification=None,
            refusal_reason=None,
        )
        sql_run, result = executed[label]
        report = pipeline.verifier.verify(case.question, generation, sql_run, result)
        c = report.confidence
        out.append(
            InjectedResult(
                id=case.id,
                kind=case.bad_kind if label == "bad" else "gold",
                label=label,
                confidence=c.score,
                likely_hallucination=c.likely_hallucination,
                components=c.components,
                reasons=c.reasons,
                input_tokens=sum(u.input_tokens for u in report.usages),
                output_tokens=sum(u.output_tokens for u in report.usages),
            )
        )
    return out


def write_counters(admin_url: str) -> dict[str, int]:
    """Rows inserted/updated/deleted per table since stats reset: must not move during eval."""
    query = (
        "SELECT relname, n_tup_ins + n_tup_upd + n_tup_del "
        "FROM pg_stat_user_tables WHERE schemaname = 'public'"
    )
    with psycopg.connect(conninfo(admin_url)) as conn:
        return {name: int(n) for name, n in conn.execute(query).fetchall()}


def run_all(
    pipeline: Text2SQLPipeline, cases: list[EvalCase], injected: bool = True
) -> dict[str, Any]:
    results = []
    for i, case in enumerate(cases, 1):
        result = run_case(pipeline, case)
        logger.warning(
            "[%d/%d] %s %s %s",
            i,
            len(cases),
            case.id,
            "PASS" if result.passed else "FAIL",
            result.detail,
        )
        results.append(result)
    injected_results: list[InjectedResult] = []
    if injected:
        for case in (c for c in cases if c.bad_sql):
            injected_results += run_injected(pipeline, case)
            logger.warning("injected %s done", case.id)
    return {"cases": results, "injected": injected_results}
