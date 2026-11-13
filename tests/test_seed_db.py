"""Integration test: load the small dataset into a real PostgreSQL."""

import psycopg
import pytest

from text2sql.db.seed import Table, _conninfo

pytestmark = pytest.mark.integration


def test_load_small_dataset(seeded_db: str, small_dataset: dict[str, Table]) -> None:
    with psycopg.connect(_conninfo(seeded_db)) as conn:
        for table in small_dataset.values():
            (count,) = conn.execute(f"SELECT COUNT(*) FROM {table.name}").fetchone()  # noqa: S608
            assert count == len(table.rows), table.name

        # Identity sequences were moved past the explicit IDs: a new insert must not collide.
        (next_id,) = conn.execute(
            "INSERT INTO categories (name) VALUES ('__test__') RETURNING category_id"
        ).fetchone()
        assert next_id == len(small_dataset["categories"].rows) + 1
        conn.rollback()

        # Column comments are in place for introspection.
        (comment,) = conn.execute(
            "SELECT col_description('order_items'::regclass, 6)"  # 6th column = discount
        ).fetchone()
        assert "net amount" in comment
