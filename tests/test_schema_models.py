"""Unit tests for schema models (no database)."""

import pytest

from text2sql.schema import ColumnInfo, DatabaseSchema, ForeignKeyInfo, TableInfo


def _table(name: str, fks: dict[str, str] | None = None) -> TableInfo:
    fks = fks or {}
    cols = [ColumnInfo(name=f"{name}_id", type="INTEGER", nullable=False, is_primary_key=True)]
    cols += [ColumnInfo(name=c, type="INTEGER", nullable=False) for c in fks]
    return TableInfo(
        name=name,
        columns=cols,
        primary_key=[f"{name}_id"],
        foreign_keys=[
            ForeignKeyInfo(columns=[c], referred_table=t, referred_columns=[c])
            for c, t in fks.items()
        ],
    )


@pytest.fixture
def schema() -> DatabaseSchema:
    return DatabaseSchema(
        tables=[
            _table("customers"),
            _table("products"),
            _table("orders", {"customer_id": "customers"}),
            _table("order_items", {"order_id": "orders", "product_id": "products"}),
        ]
    )


def test_neighbors_follow_fks_in_both_directions(schema: DatabaseSchema) -> None:
    assert schema.neighbors("order_items") == {"orders", "products"}
    assert schema.neighbors("orders") == {"customers", "order_items"}
    assert schema.neighbors("products") == {"order_items"}


def test_subset_keeps_order_and_filters(schema: DatabaseSchema) -> None:
    sub = schema.subset({"order_items", "customers"})
    assert sub.table_names == ["customers", "order_items"]


def test_lookup_errors_are_explicit(schema: DatabaseSchema) -> None:
    with pytest.raises(KeyError):
        schema.table("nope")
    with pytest.raises(KeyError):
        schema.table("orders").column("nope")


def test_schema_roundtrips_through_json(schema: DatabaseSchema) -> None:
    assert DatabaseSchema.model_validate_json(schema.model_dump_json()) == schema
