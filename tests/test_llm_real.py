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
