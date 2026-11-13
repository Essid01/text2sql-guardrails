"""Unit tests for the structured output model and the generator (fake LLM)."""

from typing import Any

import pytest
from pydantic import ValidationError

from text2sql.generation import ClarificationRequest, SQLGeneration, SQLGenerator
from text2sql.llm import FakeLLMClient
from text2sql.prompt.builder import PromptBuilder
from text2sql.schema import ColumnInfo, DatabaseSchema, TableInfo


def _gen(**overrides: Any) -> SQLGeneration:
    base: dict[str, Any] = {
        "kind": "sql",
        "explanation": "Counts orders.",
        "sql": "SELECT COUNT(*) FROM orders",
        "confidence": 0.9,
        "tables_used": ["orders"],
        "columns_used": [],
        "clarification": None,
        "refusal_reason": None,
    }
    return SQLGeneration(**{**base, **overrides})


def test_valid_sql_output() -> None:
    assert _gen().kind == "sql"


@pytest.mark.parametrize(
    "overrides",
    [
        {"sql": None},
        {"sql": "   "},
        {"confidence": 1.5},
        {"confidence": -0.1},
        {"refusal_reason": "nope"},
        {"kind": "clarification"},  # no clarification object
        {"kind": "clarification", "sql": None,
         "clarification": ClarificationRequest(question="Which?", options=["only one"])},
        {"kind": "refusal", "sql": None},  # no reason
        {"kind": "refusal", "refusal_reason": "read-only"},  # sql still present
    ],
)  # fmt: skip
def test_inconsistent_outputs_are_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _gen(**overrides)


def test_valid_clarification_and_refusal() -> None:
    clar = _gen(kind="clarification", sql=None,
                clarification=ClarificationRequest(question="Gross or net?",
                                                   options=["gross", "net"]))  # fmt: skip
    ref = _gen(kind="refusal", sql=None, refusal_reason="No supplier data in the schema.")
    assert clar.clarification.options == ["gross", "net"]
    assert ref.refusal_reason


def test_schema_has_no_optional_fields() -> None:
    """Every field is required (nullable when needed): explicit choices, strict schema."""
    schema = SQLGeneration.model_json_schema()
    assert set(schema["required"]) == set(schema["properties"])


def test_generator_sends_built_prompt_and_returns_metadata() -> None:
    schema = DatabaseSchema(
        tables=[TableInfo(name="orders", columns=[ColumnInfo(name="id", type="INTEGER",
                                                             nullable=False)])]
    )  # fmt: skip
    fake = FakeLLMClient(lambda s, u, m: _gen())
    result = SQLGenerator(PromptBuilder(schema, examples=[]), fake).generate("count orders")
    system, user = fake.calls[0]
    assert "PostgreSQL" in system
    assert user.endswith("<question>\ncount orders\n</question>")
    assert result.output.sql == "SELECT COUNT(*) FROM orders"
    assert result.prompt.tables == ["orders"]
