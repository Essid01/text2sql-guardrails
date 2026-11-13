"""Integration test: load the small dataset into a real PostgreSQL."""

import psycopg
import pytest

from text2sql.db.seed import SeedConfig, _conninfo, generate, load

pytestmark = pytest.mark.integration


def test_load_small_dataset(database_url: str) -> None:
    tables = generate(SeedConfig.small())
    load(database_url, tables)

    with psycopg.connect(_conninfo(database_url)) as conn:
        for table in tables.values():
            (count,) = conn.execute(f"SELECT COUNT(*) FROM {table.name}").fetchone()  # noqa: S608
            assert count == len(table.rows), table.name

        # Identity sequences were moved past the explicit IDs: a new insert must not collide.
        (next_id,) = conn.execute(
            "INSERT INTO categories (name) VALUES ('__test__') RETURNING category_id"
        ).fetchone()
        assert next_id == len(tables["categories"].rows) + 1
        conn.rollback()

        # Column comments are in place for introspection.
        (comment,) = conn.execute(
            "SELECT col_description('order_items'::regclass, 6)"  # 6th column = discount
        ).fetchone()
        assert "net amount" in comment
