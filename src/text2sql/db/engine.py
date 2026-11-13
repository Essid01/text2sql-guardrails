"""SQLAlchemy engine factory."""

from functools import lru_cache

from sqlalchemy import Engine, create_engine


@lru_cache(maxsize=4)
def get_engine(database_url: str) -> Engine:
    """Return a pooled engine for `database_url` (one per URL per process)."""
    return create_engine(database_url, pool_pre_ping=True)
