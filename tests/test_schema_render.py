"""Unit tests for DDL rendering of the schema."""

from text2sql.prompt.schema_render import render_schema, render_table
from text2sql.schema import ColumnInfo, DatabaseSchema, ForeignKeyInfo, TableInfo


def test_render_table_with_comments_fk_and_values() -> None:
    table = TableInfo(
        name="orders",
        comment="Customer orders.",
        row_count_estimate=40000,
        primary_key=["order_id"],
        columns=[
            ColumnInfo(name="order_id", type="INTEGER", nullable=False, is_primary_key=True),
            ColumnInfo(name="customer_id", type="INTEGER", nullable=False),
            ColumnInfo(name="status", type="TEXT", nullable=False,
                       comment="Lifecycle.", sample_values=["completed", "it's odd"]),
        ],
        foreign_keys=[ForeignKeyInfo(columns=["customer_id"], referred_table="customers",
                                     referred_columns=["customer_id"])],
    )  # fmt: skip
    assert render_table(table) == (
        "-- Customer orders.\n"
        "-- ~40,000 rows\n"
        "CREATE TABLE orders (\n"
        "  order_id INTEGER PRIMARY KEY,\n"
        "  customer_id INTEGER NOT NULL REFERENCES customers(customer_id),\n"
        "  status TEXT NOT NULL -- Lifecycle. Values: 'completed', 'it''s odd'\n"
        ");"
    )


def test_composite_primary_key_is_rendered_as_constraint() -> None:
    table = TableInfo(
        name="inventory",
        primary_key=["store_id", "product_id"],
        columns=[
            ColumnInfo(name="store_id", type="INTEGER", nullable=False, is_primary_key=True),
            ColumnInfo(name="product_id", type="INTEGER", nullable=False, is_primary_key=True),
        ],
    )
    rendered = render_table(table)
    assert "PRIMARY KEY (store_id, product_id)\n);" in rendered
    assert "store_id INTEGER NOT NULL," in rendered  # no inline PRIMARY KEY on composite


def test_render_schema_joins_tables() -> None:
    schema = DatabaseSchema(
        tables=[
            TableInfo(name="a", columns=[ColumnInfo(name="x", type="INTEGER", nullable=True)]),
            TableInfo(name="b", columns=[ColumnInfo(name="y", type="TEXT", nullable=True)]),
        ]
    )
    assert render_schema(schema) == (
        "CREATE TABLE a (\n  x INTEGER\n);\n\nCREATE TABLE b (\n  y TEXT\n);"
    )
