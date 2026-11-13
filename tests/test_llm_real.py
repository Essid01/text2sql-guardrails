"""One real call to the configured model (~$0.0002). Never run in CI.

Run explicitly with: pytest -m llm
"""

import pytest
from pydantic import BaseModel

from text2sql.config import Settings
from text2sql.llm.client import AnthropicClient

pytestmark = pytest.mark.llm


class Answer(BaseModel):
    sql: str


def test_real_structured_call() -> None:
    settings = Settings()
    try:
        api_key, model = settings.require_llm()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    client = AnthropicClient(
        api_key=api_key,
        model=model,
        effort=settings.llm_effort,
        max_tokens=settings.llm_max_tokens,
        price_input_per_mtok=settings.llm_price_input_per_mtok,
        price_output_per_mtok=settings.llm_price_output_per_mtok,
    )
    out, usage = client.complete(
        "Answer with a PostgreSQL query only.", "Select the number one.", Answer
    )
    assert "select" in out.sql.lower()
    assert usage.input_tokens > 0 and usage.cost_usd > 0


def test_real_sql_generation_schema_is_accepted() -> None:
    """The SQLGeneration schema works with constrained decoding on the configured model."""
    from text2sql.generation import SQLGeneration, SQLGenerator
    from text2sql.prompt.builder import PromptBuilder
    from text2sql.schema import ColumnInfo, DatabaseSchema, TableInfo

    settings = Settings()
    try:
        api_key, model = settings.require_llm()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    schema = DatabaseSchema(
        tables=[
            TableInfo(
                name="orders",
                columns=[
                    ColumnInfo(name="order_id", type="INTEGER", nullable=False),
                    ColumnInfo(name="status", type="TEXT", nullable=False,
                               sample_values=["cancelled", "completed"]),
                ],
            )
        ]
    )  # fmt: skip
    client = AnthropicClient(api_key=api_key, model=model, effort=settings.llm_effort,
                             max_tokens=settings.llm_max_tokens)  # fmt: skip
    result = SQLGenerator(PromptBuilder(schema, examples=[]), client).generate(
        "How many cancelled orders are there?"
    )
    assert isinstance(result.output, SQLGeneration)
    assert result.output.kind == "sql"
    assert "cancelled" in result.output.sql
