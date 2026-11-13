"""Shared fixtures."""

import psycopg
import pytest

from text2sql.config import get_settings
from text2sql.db.seed import _conninfo


@pytest.fixture(scope="session")
def database_url() -> str:
    """URL of a reachable PostgreSQL, or skip the test (integration tests only)."""
    url = get_settings().database_url
    try:
        with psycopg.connect(_conninfo(url), connect_timeout=3):
            pass
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL not reachable: {exc.__class__.__name__}")
    return url
