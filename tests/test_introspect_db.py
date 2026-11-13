"""Integration tests: introspect the seeded test database."""

import pytest

from text2sql.db.engine import get_engine
from text2sql.schema import DatabaseSchema, introspect

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def schema(seeded_db: str) -> DatabaseSchema:
    return introspect(get_engine(seeded_db))


def test_all_tables_found(schema: DatabaseSchema) -> None:
    assert schema.table_names == sorted(
        ["categories", "customers", "inventory", "order_items", "orders", "products", "stores"]
    )


def test_primary_keys(schema: DatabaseSchema) -> None:
    assert schema.table("orders").primary_key == ["order_id"]
    assert schema.table("inventory").primary_key == ["store_id", "product_id"]  # composite
    assert schema.table("orders").column("order_id").is_primary_key


def test_foreign_keys(schema: DatabaseSchema) -> None:
    fks = {
        (tuple(fk.columns), fk.referred_table) for fk in schema.table("order_items").foreign_keys
    }
    assert fks == {(("order_id",), "orders"), (("product_id",), "products")}
    assert schema.neighbors("orders") == {"customers", "stores", "order_items"}


def test_types_and_nullability(schema: DatabaseSchema) -> None:
    orders = schema.table("orders")
    assert orders.column("order_date").type.startswith("TIMESTAMP")
    assert orders.column("shipped_date").nullable is True
    assert orders.column("status").nullable is False
    assert schema.table("order_items").column("discount").type == "NUMERIC(4, 3)"


def test_comments_are_read(schema: DatabaseSchema) -> None:
    assert schema.table("orders").comment
    assert "net amount" in schema.table("order_items").column("discount").comment


def test_categorical_columns_have_sample_values(schema: DatabaseSchema) -> None:
    assert schema.table("orders").column("status").sample_values == [
        "cancelled",
        "completed",
        "pending",
        "returned",
    ]
    assert schema.table("stores").column("store_type").sample_values == ["online", "physical"]
    assert "gold" in schema.table("customers").column("loyalty_tier").sample_values


def test_high_cardinality_and_non_text_columns_have_no_samples(schema: DatabaseSchema) -> None:
    assert schema.table("customers").column("email").sample_values is None  # unique text
    assert schema.table("orders").column("order_id").sample_values is None  # integer


def test_row_count_estimates_after_analyze(schema: DatabaseSchema) -> None:
    assert schema.table("orders").row_count_estimate == 800
