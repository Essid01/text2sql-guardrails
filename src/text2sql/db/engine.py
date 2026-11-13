"""SQLAlchemy engine factory."""

from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url


@lru_cache(maxsize=4)
def get_engine(database_url: str) -> Engine:
    """Return a pooled engine for `database_url` (one per URL per process)."""
    return create_engine(database_url, pool_pre_ping=True)


def conninfo(database_url: str) -> str:
    """Convert a SQLAlchemy URL (postgresql+psycopg://...) to a libpq conninfo for psycopg."""
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
