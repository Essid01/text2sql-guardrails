"""Integration test: every few-shot SQL runs on the real schema (read-only)."""

import psycopg
import pytest

from text2sql.db.seed import _conninfo
from text2sql.prompt.builder import load_examples

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("example", load_examples(), ids=lambda e: e.id)
def test_example_sql_executes(seeded_db: str, example) -> None:
    with psycopg.connect(_conninfo(seeded_db)) as conn:
        conn.read_only = True
        cursor = conn.execute(example.sql)
        # Row counts depend on dataset size; what matters is that the SQL is valid
        # against the real schema and returns named columns.
        assert cursor.description is not None
        assert all(col.name != "?column?" for col in cursor.description), "unnamed column"
