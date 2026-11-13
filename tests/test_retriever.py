"""Unit tests for schema retrieval, using the deterministic hashing embedder."""

import numpy as np
import pytest

from text2sql.schema import ColumnInfo, DatabaseSchema, ForeignKeyInfo, TableInfo
from text2sql.schema.embedder import HashingEmbedder
from text2sql.schema.retriever import SchemaRetriever, shortest_fk_path, table_document


def _t(
    name: str, cols: list[str], fks: dict[str, str] | None = None, comment: str = ""
) -> TableInfo:
    fks = fks or {}
    return TableInfo(
        name=name,
        comment=comment or None,
        columns=[ColumnInfo(name=c, type="TEXT", nullable=True) for c in cols]
        + [ColumnInfo(name=c, type="INTEGER", nullable=False) for c in fks],
        foreign_keys=[
            ForeignKeyInfo(columns=[c], referred_table=t, referred_columns=[c])
            for c, t in fks.items()
        ],
    )


@pytest.fixture
def schema() -> DatabaseSchema:
    """Mini retail schema: categories <- products <- order_items -> orders -> customers."""
    return DatabaseSchema(
        tables=[
            _t("categories", ["category_name"], comment="Product categories"),
            _t("customers", ["first_name", "city"], comment="Registered customers"),
            _t("order_items", ["quantity", "unit_price"], {"order_id": "orders",
                                                           "product_id": "products"}),
            _t("orders", ["order_date", "status"], {"customer_id": "customers"}),
            _t("products", ["product_name", "brand"], {"category_id": "categories"}),
            _t("suppliers", ["supplier_name"]),  # isolated: no FK
        ]
    )  # fmt: skip


def test_hashing_embedder_is_normalized_and_deterministic() -> None:
    emb = HashingEmbedder()
    a, b = emb.embed(["orders per city", "orders per city"])
    assert np.allclose(a, b)
    assert np.isclose(np.linalg.norm(a), 1.0)
    assert emb.embed([""])[0].sum() == 0  # empty text -> zero vector, no division by zero


def test_table_document_contains_names_comments_and_values() -> None:
    table = TableInfo(
        name="orders",
        comment="Customer orders",
        columns=[
            ColumnInfo(name="order_status", type="TEXT", nullable=False,
                       comment="Lifecycle", sample_values=["completed", "cancelled"]),
        ],
    )  # fmt: skip
    doc = table_document(table)
    assert "Table orders." in doc
    assert "Customer orders" in doc
    assert "order status (Lifecycle) values: completed, cancelled." in doc


def test_shortest_fk_path(schema: DatabaseSchema) -> None:
    assert shortest_fk_path(schema, "categories", "orders") == [
        "categories",
        "products",
        "order_items",
        "orders",
    ]
    assert shortest_fk_path(schema, "orders", "orders") == ["orders"]
    assert shortest_fk_path(schema, "orders", "suppliers") is None


def test_top_k_picks_most_similar_tables(schema: DatabaseSchema) -> None:
    retriever = SchemaRetriever(schema, HashingEmbedder(), top_k=1)
    result = retriever.retrieve("customers by city")
    assert result.by_similarity == ["customers"]
    assert result.tables == ["customers"]
    assert result.added_as_bridge == []


def test_bridge_tables_are_added_for_joins(schema: DatabaseSchema) -> None:
    retriever = SchemaRetriever(schema, HashingEmbedder(), top_k=2)
    result = retriever.retrieve("customers categories")
    assert set(result.by_similarity) == {"customers", "categories"}
    # customers -> orders -> order_items -> products -> categories
    assert result.added_as_bridge == ["order_items", "orders", "products"]
    assert result.tables == ["categories", "customers", "order_items", "orders", "products"]


def test_unreachable_tables_are_kept_without_bridge(schema: DatabaseSchema) -> None:
    retriever = SchemaRetriever(schema, HashingEmbedder(), top_k=2)
    result = retriever.retrieve("supplier name and customers")
    assert set(result.tables) == {"suppliers", "customers"}


def test_top_k_none_returns_full_schema(schema: DatabaseSchema) -> None:
    result = SchemaRetriever(schema, HashingEmbedder(), top_k=None).retrieve("anything")
    assert result.tables == schema.table_names
    assert set(result.scores) == set(schema.table_names)
