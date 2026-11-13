"""Unit tests for the composite confidence score and the verifier."""

from typing import Any

import pytest
from pydantic import BaseModel

from text2sql.execution import QueryResult
from text2sql.generation import SQLGeneration
from text2sql.llm import FakeLLMClient
from text2sql.schema import ColumnInfo, DatabaseSchema, TableInfo
from text2sql.verification.backtranslation import (
    AlignmentJudgement,
    BackTranslation,
    BackTranslator,
)
from text2sql.verification.confidence import compute_confidence, sanity_signal
from text2sql.verification.consensus import Agreement
from text2sql.verification.sanity import Finding
from text2sql.verification.verifier import Verifier


def _f(check: str, severity: str = "warning") -> Finding:
    return Finding(check=check, severity=severity, message=check)


def test_all_signals_good_gives_high_confidence() -> None:
    report = compute_confidence(findings=[], llm_confidence=0.9, alignment=1.0,
                                agreement=Agreement.MATCH)  # fmt: skip
    assert report.score == pytest.approx(0.99)
    assert report.level == "high" and not report.likely_hallucination
    assert report.reasons == []


def test_misaligned_and_mismatch_flag_hallucination() -> None:
    report = compute_confidence(findings=[], llm_confidence=0.95, alignment=0.0,
                                agreement=Agreement.MISMATCH)  # fmt: skip
    assert report.likely_hallucination and report.level == "low"
    assert len(report.reasons) == 2


def test_self_confidence_alone_cannot_save_a_wrong_answer() -> None:
    """A confident model with a misaligned query must still be flagged."""
    report = compute_confidence(findings=[], llm_confidence=1.0, alignment=0.0,
                                agreement=Agreement.UNAVAILABLE)  # fmt: skip
    assert report.likely_hallucination


def test_unavailable_signals_are_excluded_not_counted_as_zero() -> None:
    report = compute_confidence(findings=[], llm_confidence=0.9, alignment=None,
                                agreement=Agreement.UNAVAILABLE)  # fmt: skip
    assert report.components["alignment"] is None and report.components["agreement"] is None
    assert report.score == pytest.approx((0.2 * 1 + 0.1 * 1 + 0.1 * 0.9) / 0.4)


def test_sanity_signal() -> None:
    assert sanity_signal([]) == 1.0
    assert sanity_signal([_f("null_column")]) == 0.75
    assert sanity_signal([_f("negative_value", "error")]) == 0.0
    assert sanity_signal([_f("truncated", "info")]) == 1.0
    assert sanity_signal([_f("declared_tables_mismatch")]) == 1.0  # counted elsewhere


def test_self_consistency_lowers_schema_coverage() -> None:
    report = compute_confidence(findings=[_f("declared_tables_mismatch")], llm_confidence=0.9)
    assert report.components["schema_coverage"] == 0.5


SCHEMA = DatabaseSchema(tables=[TableInfo(name="orders", columns=[
    ColumnInfo(name="status", type="TEXT", nullable=False)])])  # fmt: skip
GEN = SQLGeneration(kind="sql", explanation="e", sql="SELECT COUNT(*) AS n FROM orders",
                    confidence=0.9, tables_used=["orders"], columns_used=[],
                    clarification=None, refusal_reason=None)  # fmt: skip
RESULT = QueryResult(columns=["n"], rows=[[5]], row_count=1, truncated=False, duration_ms=1,
                     estimated_cost=1, plan={})  # fmt: skip


def _bt(score: float) -> BackTranslator:
    def responder(system: str, user: str, model: type[BaseModel]) -> Any:
        if model is BackTranslation:
            return BackTranslation(question="How many orders are there?")
        return AlignmentJudgement(differences=[], aligned=score > 0.5, score=score)

    return BackTranslator(FakeLLMClient(responder), SCHEMA)


def test_verifier_sanity_only() -> None:
    report = Verifier(SCHEMA).verify("How many orders?", GEN, "SELECT ...", RESULT)
    assert report.backtranslation is None and report.consensus is None
    assert not report.confidence.likely_hallucination
    assert report.usages == []


def test_verifier_with_backtranslation() -> None:
    good = Verifier(SCHEMA, backtranslator=_bt(1.0)).verify("How many orders?", GEN, "", RESULT)
    bad = Verifier(SCHEMA, backtranslator=_bt(0.0)).verify("Revenue in 2025?", GEN, "", RESULT)
    assert good.confidence.score > bad.confidence.score
    assert bad.confidence.likely_hallucination
    assert len(good.usages) == 2
