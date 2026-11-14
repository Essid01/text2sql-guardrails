"""Export of incorrect answers as candidate evaluation cases."""

import yaml

from text2sql.feedback import export
from text2sql.history import FeedbackRequest, InMemoryHistoryStore
from text2sql.pipeline import PipelineResponse, Status


def test_export_writes_reviewable_candidates(tmp_path) -> None:
    store = InMemoryHistoryStore()
    for q in ["good question", "bad question"]:
        store.record(PipelineResponse(question=q, status=Status.EXECUTED, sql_executed="SELECT 1"))
    store.set_feedback(1, FeedbackRequest(verdict="correct"))
    store.set_feedback(2, FeedbackRequest(verdict="incorrect", comment="should be 2025",
                                          corrected_sql="SELECT 2"))  # fmt: skip
    out = tmp_path / "evals" / "candidates.yaml"
    assert export(store, out) == 1
    (case,) = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert case["id"] == "feedback-2" and case["question"] == "bad question"
    assert case["status"] == "to_review"  # never gold data without human review
    assert case["user_corrected_sql"] == "SELECT 2"


def test_export_with_no_feedback(tmp_path) -> None:
    out = tmp_path / "c.yaml"
    assert export(InMemoryHistoryStore(), out) == 0
    assert yaml.safe_load(out.read_text(encoding="utf-8")) == []
