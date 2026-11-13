"""Unit tests for prompt assembly (no database, no LLM)."""

import pytest

from text2sql.prompt.builder import (
    FewShotExample,
    PromptBuilder,
    load_examples,
    load_system_prompt,
)
from text2sql.schema import ColumnInfo, DatabaseSchema, ForeignKeyInfo, TableInfo
from text2sql.schema.embedder import HashingEmbedder
from text2sql.schema.retriever import SchemaRetriever


@pytest.fixture
def schema() -> DatabaseSchema:
    def t(name: str, cols: list[str], fks: dict[str, str] | None = None) -> TableInfo:
        fks = fks or {}
        return TableInfo(
            name=name,
            columns=[ColumnInfo(name=c, type="TEXT", nullable=True) for c in [*cols, *fks]],
            foreign_keys=[
                ForeignKeyInfo(columns=[c], referred_table=r, referred_columns=[c])
                for c, r in fks.items()
            ],
        )

    return DatabaseSchema(
        tables=[
            t("customers", ["customer_id", "city"]),
            t("orders", ["order_id", "status"], {"customer_id": "customers"}),
            t("stores", ["store_id", "store_type"]),
        ]
    )


EXAMPLES = [
    FewShotExample(id="a", question="How many stores are online?", sql="SELECT 1;"),
    FewShotExample(id="b", question="Cancelled orders per customer city", sql="SELECT 2;"),
    FewShotExample(id="c", question="List gold customers", sql="SELECT 3;"),
]


def test_system_prompt_is_complete() -> None:
    system = load_system_prompt()
    assert "TODO" not in system and "<!--" not in system
    assert "PostgreSQL" in system


def test_example_pool_is_valid() -> None:
    examples = load_examples()
    assert len(examples) >= 3
    assert len({e.id for e in examples}) == len(examples)
    assert all(e.sql.strip().upper().startswith(("SELECT", "WITH")) for e in examples)


def test_user_message_layout(schema: DatabaseSchema) -> None:
    prompt = PromptBuilder(schema, EXAMPLES, n_examples=2).build("  orders per city  ")
    assert prompt.user.startswith("<schema>\nCREATE TABLE customers")
    assert prompt.user.endswith("<question>\norders per city\n</question>")  # question last
    assert prompt.user.index("<examples>") < prompt.user.index("<question>")
    assert prompt.example_ids == ["a", "b"]  # no embedder: pool order
    assert prompt.tables == ["customers", "orders", "stores"]
    assert "TODO" not in prompt.system


def test_examples_selected_by_similarity(schema: DatabaseSchema) -> None:
    builder = PromptBuilder(schema, EXAMPLES, embedder=HashingEmbedder(), n_examples=1)
    assert builder.build("cancelled orders by city").example_ids == ["b"]
    assert builder.build("online stores count").example_ids == ["a"]


def test_retriever_restricts_schema(schema: DatabaseSchema) -> None:
    retriever = SchemaRetriever(schema, HashingEmbedder(), top_k=1)
    prompt = PromptBuilder(schema, EXAMPLES, retriever=retriever).build("store type")
    assert prompt.tables == ["stores"]
    assert "CREATE TABLE stores" in prompt.user
    assert "CREATE TABLE orders" not in prompt.user


def test_no_examples_section_when_pool_empty(schema: DatabaseSchema) -> None:
    prompt = PromptBuilder(schema, examples=[]).build("anything")
    assert "<examples>" not in prompt.user
    assert prompt.example_ids == []


def test_empty_question_rejected(schema: DatabaseSchema) -> None:
    with pytest.raises(ValueError, match="empty"):
        PromptBuilder(schema, EXAMPLES).build("   ")
