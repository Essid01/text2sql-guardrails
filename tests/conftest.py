"""Shared fixtures.

Integration tests never touch the main `retail` database: they use a separate
`<db>_test` database, created on demand and loaded with the small dataset.
"""

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import make_url

from text2sql.config import get_settings
from text2sql.db.seed import SeedConfig, Table, _conninfo, generate, load


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Real LLM calls cost money: run them only when explicitly selected with `-m llm`.

    A marker filter in `addopts` is not enough: any `-m` on the command line replaces it.
    """
    if "llm" in (config.getoption("markexpr") or "").replace("not llm", ""):
        return
    skip = pytest.mark.skip(reason="real LLM call: run with `pytest -m llm`")
    for item in items:
        if "llm" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def database_url() -> str:
    """URL of a reachable test database (`<db>_test`), or skip the test."""
    base_url = make_url(get_settings().database_url)
    test_db = f"{base_url.database}_test"
    try:
        with psycopg.connect(
            _conninfo(base_url.render_as_string(hide_password=False)),
            connect_timeout=3,
            autocommit=True,  # CREATE DATABASE cannot run inside a transaction
        ) as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (test_db,)
            ).fetchone()
            if not exists:
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(test_db)))
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL not reachable: {exc.__class__.__name__}")
    return base_url.set(database=test_db).render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def small_dataset() -> dict[str, Table]:
    return generate(SeedConfig.small())


@pytest.fixture(scope="session")
def seeded_db(database_url: str, small_dataset: dict[str, Table]) -> str:
    """Test database loaded once per session with the small dataset."""
    load(database_url, small_dataset)
    return database_url


@pytest.fixture(scope="session")
def readonly_url(seeded_db: str) -> str:
    """Read-only role URL on the test database, role created like the seed script does.

    Roles are cluster-wide: reuse the configured reader credentials so the tests never
    change the password used by the main database.
    """
    from text2sql.db.roles import setup_readonly_role

    configured = get_settings().readonly_database_url or (
        "postgresql+psycopg://text2sql_reader:reader-local-dev@localhost:5432/retail"
    )
    url = make_url(configured).set(database=make_url(seeded_db).database)
    rendered = url.render_as_string(hide_password=False)
    setup_readonly_role(seeded_db, rendered)
    return rendered
