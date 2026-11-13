"""Read the live database schema with SQLAlchemy's inspector."""

from __future__ import annotations

import logging

from sqlalchemy import Connection, Engine, column, func, inspect, select, table, text
from sqlalchemy.types import Enum, String

from text2sql.schema.models import ColumnInfo, DatabaseSchema, ForeignKeyInfo, TableInfo

logger = logging.getLogger(__name__)

DEFAULT_MAX_CATEGORICAL = 20


def introspect(
    engine: Engine,
    schema: str = "public",
    max_categorical: int = DEFAULT_MAX_CATEGORICAL,
) -> DatabaseSchema:
    """Describe every table of `schema`: columns, types, keys, comments, sample values.

    A text column is treated as categorical when it has at most `max_categorical`
    distinct non-null values; those values are kept so the LLM can write exact
    literals (`status = 'completed'`, not `'done'`).
    """
    inspector = inspect(engine)
    row_estimates = _row_count_estimates(engine, schema)
    tables: list[TableInfo] = []

    with engine.connect() as conn:
        for table_name in sorted(inspector.get_table_names(schema=schema)):
            pk = inspector.get_pk_constraint(table_name, schema=schema)["constrained_columns"]
            columns: list[ColumnInfo] = []
            for col in inspector.get_columns(table_name, schema=schema):
                samples = None
                if isinstance(col["type"], String | Enum):
                    samples = _distinct_values(conn, table_name, col["name"], max_categorical)
                columns.append(
                    ColumnInfo(
                        name=col["name"],
                        type=str(col["type"]),
                        nullable=col["nullable"],
                        is_primary_key=col["name"] in pk,
                        comment=col.get("comment"),
                        sample_values=samples,
                    )
                )
            fks = [
                ForeignKeyInfo(
                    columns=fk["constrained_columns"],
                    referred_table=fk["referred_table"],
                    referred_columns=fk["referred_columns"],
                )
                for fk in inspector.get_foreign_keys(table_name, schema=schema)
            ]
            tables.append(
                TableInfo(
                    name=table_name,
                    comment=inspector.get_table_comment(table_name, schema=schema).get("text"),
                    columns=columns,
                    primary_key=pk,
                    foreign_keys=fks,
                    row_count_estimate=row_estimates.get(table_name),
                )
            )

    logger.info("introspected schema=%s tables=%d", schema, len(tables))
    return DatabaseSchema(tables=tables)


def _distinct_values(
    conn: Connection, table_name: str, column_name: str, limit: int
) -> list[str] | None:
    """Return sorted distinct values if there are at most `limit`, else None."""
    col = column(column_name)
    # Identifiers are quoted by SQLAlchemy: no string formatting of SQL.
    distinct = (
        select(col).select_from(table(table_name)).where(col.is_not(None)).distinct().subquery()
    )
    (n_distinct,) = conn.execute(
        select(func.count()).select_from(select(distinct).limit(limit + 1).subquery())
    ).one()
    if n_distinct == 0 or n_distinct > limit:
        return None
    rows = conn.execute(select(distinct).order_by(distinct.c[0])).scalars().all()
    return [str(v) for v in rows]


def _row_count_estimates(engine: Engine, schema: str) -> dict[str, int]:
    """Planner row estimates from pg_class (instant, accurate after ANALYZE)."""
    if engine.dialect.name != "postgresql":
        return {}
    query = text(
        "SELECT c.relname, c.reltuples::bigint FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = :schema AND c.relkind = 'r'"
    )
    with engine.connect() as conn:
        # reltuples = -1 means "never analyzed": report unknown rather than 0.
        rows = conn.execute(query, {"schema": schema})
        return {name: int(n) for name, n in rows if n >= 0}
