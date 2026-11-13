"""Read-only database role used to execute generated SQL (second line of defense).

Even if a malicious query got past the guardrails, this role cannot write: it only has
SELECT on the demo tables, its transactions are read-only by default and long queries
are cancelled by a statement timeout.
"""

from __future__ import annotations

import logging

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from text2sql.db.engine import conninfo

logger = logging.getLogger(__name__)


def setup_readonly_role(admin_url: str, readonly_url: str, statement_timeout: str = "5s") -> None:
    """Create or update the role described by `readonly_url` (user, password, database).

    Must run as an admin after every (re)load: dropping and recreating tables drops
    their grants. Idempotent.
    """
    ro = make_url(readonly_url)
    if not ro.username or not ro.password:
        raise ValueError("READONLY_DATABASE_URL must contain a user and a password")
    role, database = sql.Identifier(ro.username), sql.Identifier(ro.database or "")
    password = sql.Literal(ro.password)

    with psycopg.connect(conninfo(admin_url), autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (ro.username,)
        ).fetchone()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(
            sql.SQL(verb + " ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOINHERIT CONNECTION LIMIT 20").format(role, password)
        )  # fmt: skip
        statements = [
            # Session defaults: every transaction is read-only and time-limited.
            sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(role),
            sql.SQL("ALTER ROLE {} SET statement_timeout = {}").format(
                role, sql.Literal(statement_timeout)
            ),
            sql.SQL("ALTER ROLE {} SET search_path = public").format(role),
            # Privileges: connect + read the demo tables, nothing else.
            sql.SQL("REVOKE CREATE ON SCHEMA public FROM PUBLIC"),
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, role),
            sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role),
            sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}").format(role),
            sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(role),
            sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {}").format(role),
        ]
        for statement in statements:
            conn.execute(statement)
    logger.info("read-only role ready role=%s database=%s", ro.username, ro.database)
