"""Integration tests: the database itself refuses writes from the read-only role.

These tests deliberately bypass the guardrails: they prove the second line of defense
holds on its own.
"""

import psycopg
import pytest
from psycopg import errors

from text2sql.db.engine import conninfo

pytestmark = pytest.mark.integration


@pytest.fixture
def reader(readonly_url: str):
    with psycopg.connect(conninfo(readonly_url), autocommit=True) as conn:
        yield conn


def test_reader_can_select(reader) -> None:
    (n,) = reader.execute("SELECT COUNT(*) FROM orders").fetchone()
    assert n > 0


def test_session_is_read_only_by_default(reader) -> None:
    (value,) = reader.execute("SHOW default_transaction_read_only").fetchone()
    assert value == "on"


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM orders",
        "UPDATE orders SET status = 'completed'",
        "INSERT INTO categories (name) VALUES ('hacked')",
        "DROP TABLE customers",
        "TRUNCATE orders",
        "CREATE TABLE evil (id int)",
        "ALTER TABLE orders ADD COLUMN evil int",
    ],
)
def test_writes_rejected_by_read_only_transaction(reader, statement: str) -> None:
    with pytest.raises((errors.ReadOnlySqlTransaction, errors.InsufficientPrivilege)):
        reader.execute(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM orders",
        "INSERT INTO categories (name) VALUES ('hacked')",
        "DROP TABLE customers",
        "CREATE TABLE evil (id int)",
    ],
)
def test_writes_rejected_by_privileges_even_in_read_write_mode(reader, statement: str) -> None:
    """An attacker who turns read-only off still lacks the privileges."""
    with reader.transaction():
        reader.execute("SET TRANSACTION READ WRITE")
        with pytest.raises(errors.InsufficientPrivilege):
            reader.execute(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT pg_ls_dir('.')",
        "SELECT nextval(pg_get_serial_sequence('orders', 'order_id'))",
        "SELECT * FROM pg_authid",
    ],
)
def test_dangerous_functions_rejected(reader, statement: str) -> None:
    with pytest.raises((errors.InsufficientPrivilege, errors.ReadOnlySqlTransaction)):
        reader.execute(statement)


def test_statement_timeout_applies(reader) -> None:
    (value,) = reader.execute("SHOW statement_timeout").fetchone()
    assert value == "5s"
