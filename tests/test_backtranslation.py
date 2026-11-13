"""Unit tests for back-translation (fake LLM)."""

from pydantic import BaseModel

from text2sql.llm import FakeLLMClient
from text2sql.schema import ColumnInfo, DatabaseSchema, TableInfo
from text2sql.verification.backtranslation import (
    AlignmentJudgement,
    BackTranslation,
    BackTranslator,
)

SCHEMA = DatabaseSchema(tables=[
    TableInfo(name=n, columns=[ColumnInfo(name="id", type="INTEGER", nullable=False)])
    for n in ("orders", "customers", "stores")
])  # fmt: skip


def _fake(judgement: AlignmentJudgement) -> FakeLLMClient:
    def responder(system: str, user: str, model: type[BaseModel]) -> BaseModel:
        if model is BackTranslation:
            return BackTranslation(question="How many orders were placed in 2024?")
        return judgement

    return FakeLLMClient(responder)


def test_translator_is_blind_and_judge_never_sees_sql() -> None:
    fake = _fake(AlignmentJudgement(differences=[], aligned=True, score=1.0))
    BackTranslator(fake, SCHEMA).check(
        "How many orders in 2024?", "SELECT COUNT(*) FROM orders", {"orders"}
    )
    (t_system, t_user), (j_system, j_user) = fake.calls
    assert "How many orders in 2024?" not in t_user  # blind translation
    assert "SELECT COUNT(*) FROM orders" in t_user
    assert "CREATE TABLE orders" in t_user and "CREATE TABLE stores" not in t_user  # subset
    assert "SELECT" not in j_user  # judge compares meanings only
    assert "How many orders in 2024?" in j_user
    assert "How many orders were placed in 2024?" in j_user


def test_result_carries_judgement_and_usage() -> None:
    judgement = AlignmentJudgement(differences=["year 2025 vs 2024"], aligned=False, score=0.1)
    result = BackTranslator(_fake(judgement), SCHEMA).check("Orders in 2025?", "SELECT 1", set())
    assert result.judgement.aligned is False
    assert result.reconstructed_question == "How many orders were placed in 2024?"
    assert len(result.usages) == 2
