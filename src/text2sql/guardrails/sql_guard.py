"""Static SQL guardrails based on a real PostgreSQL AST (sqlglot).

Design:
- Allowlist, not denylist, wherever possible: the root must be a query and every
  table must belong to the schema. Unknown statement types (VACUUM, DO, CALL...)
  are therefore rejected without having to enumerate them.
- The SQL that is returned (and executed) is regenerated from the checked AST,
  without comments: what runs is exactly what was analysed.
- This is the first line of defense. The second is the database itself
  (read-only role, read-only transaction, timeout): see the execution layer.
"""

from __future__ import annotations

import logging

import sqlglot
from pydantic import BaseModel, ConfigDict
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

logger = logging.getLogger(__name__)

DIALECT = "postgres"

# Functions that read/write files, sleep, change settings, write sequences, reach other
# servers, signal processes or execute SQL given as a string. Defense in depth: most of
# them also require privileges the read-only role does not have.
DEFAULT_BLOCKED_FUNCTIONS = frozenset({
    "pg_sleep", "pg_sleep_for", "pg_sleep_until",
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "lo_import", "lo_export", "lo_get", "lo_put", "lo_create", "lo_unlink", "lo_from_bytea",
    "set_config", "current_setting", "pg_reload_conf", "pg_rotate_logfile",
    "nextval", "setval",
    "dblink", "dblink_exec", "dblink_connect", "dblink_send_query",
    "pg_terminate_backend", "pg_cancel_backend",
    "pg_advisory_lock", "pg_advisory_xact_lock", "pg_try_advisory_lock",
    "pg_notify",
    "query_to_xml", "query_to_xml_and_xmlschema", "cursor_to_xml",
})  # fmt: skip

# Node types that modify data, structures, sessions or transactions.
WRITE_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter,
    exp.TruncateTable, exp.Copy, exp.Grant, exp.Set, exp.Transaction, exp.Commit,
    exp.Rollback, exp.Command, exp.Into, exp.Lock,
)  # fmt: skip

QUERY_ROOTS: tuple[type[exp.Expression], ...] = (exp.Select, exp.SetOperation)


class GuardrailConfig(BaseModel):
    """Tunable limits. `allowed_tables` normally comes from schema introspection."""

    model_config = ConfigDict(frozen=True)

    allowed_tables: frozenset[str]
    allowed_schemas: frozenset[str] = frozenset({"public"})
    max_rows: int = 1000
    max_subquery_depth: int = 3
    max_sql_length: int = 10_000
    blocked_functions: frozenset[str] = DEFAULT_BLOCKED_FUNCTIONS


class Violation(BaseModel):
    """Why a query was rejected."""

    rule: str
    message: str


class GuardResult(BaseModel):
    """Outcome of the check. `sql` is the rewritten, executable query, or None if blocked."""

    allowed: bool
    sql: str | None
    violations: list[Violation]


def _block(rule: str, message: str) -> list[Violation]:
    return [Violation(rule=rule, message=message)]


def check_sql(sql: str, config: GuardrailConfig) -> GuardResult:
    """Validate `sql` against every rule and return the safe rewritten query if allowed."""
    violations = _check(sql, config)
    if violations:
        for v in violations:
            logger.warning("guardrail blocked rule=%s reason=%s sql=%r", v.rule, v.message,
                           sql[:500])  # fmt: skip
        return GuardResult(allowed=False, sql=None, violations=violations)

    tree = sqlglot.parse_one(sql, read=DIALECT)
    tree = _enforce_limit(tree, config.max_rows)
    return GuardResult(allowed=True, sql=tree.sql(dialect=DIALECT, comments=False), violations=[])


def _check(sql: str, config: GuardrailConfig) -> list[Violation]:
    if len(sql) > config.max_sql_length:
        return _block("too_long", f"query longer than {config.max_sql_length} characters")

    try:
        statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
    except (ParseError, TokenError) as exc:
        return _block("parse_error", f"not valid PostgreSQL: {exc}")

    if not statements:
        return _block("empty", "no SQL statement")
    if len(statements) > 1:
        return _block(
            "multiple_statements", f"{len(statements)} statements; exactly one is allowed"
        )

    tree = statements[0]
    if not isinstance(tree, QUERY_ROOTS):
        return _block("not_select", f"only SELECT queries are allowed, got {tree.key.upper()}")

    violations: list[Violation] = []
    violations += _check_writes(tree)
    violations += _check_functions(tree, config)
    violations += _check_tables(tree, config)
    violations += _check_depth(tree, config)
    return violations


def _check_writes(tree: exp.Expression) -> list[Violation]:
    found = sorted({node.key.upper() for node in tree.find_all(*WRITE_NODES)})
    if not found:
        return []
    return _block("write_operation", f"write or locking clause in the query: {', '.join(found)}")


def _function_name(func: exp.Func) -> str:
    return (func.name if isinstance(func, exp.Anonymous) else func.sql_name()).lower()


def _check_functions(tree: exp.Expression, config: GuardrailConfig) -> list[Violation]:
    blocked = sorted({
        name for func in tree.find_all(exp.Func)
        if (name := _function_name(func)) in config.blocked_functions
    })  # fmt: skip
    if not blocked:
        return []
    return _block("blocked_function", f"forbidden function: {', '.join(blocked)}")


def _check_tables(tree: exp.Expression, config: GuardrailConfig) -> list[Violation]:
    cte_names = {cte.alias.lower() for cte in tree.find_all(exp.CTE)}
    unknown: set[str] = set()
    for table in tree.find_all(exp.Table):
        if isinstance(table.this, exp.Func):
            continue  # table function (generate_series...): covered by the function rule
        name, schema = table.name.lower(), table.db.lower()
        if schema and schema not in config.allowed_schemas:
            unknown.add(f"{schema}.{name}")
        elif name not in config.allowed_tables and not (not schema and name in cte_names):
            unknown.add(f"{schema}.{name}" if schema else name)
    if not unknown:
        return []
    return _block("unknown_table", f"tables not in the schema: {', '.join(sorted(unknown))}")


def _select_depth(node: exp.Expression) -> int:
    """Number of SELECTs strictly enclosing `node`."""
    depth, parent = 0, node.parent
    while parent is not None:
        if isinstance(parent, exp.Select):
            depth += 1
        parent = parent.parent
    return depth


def _check_depth(tree: exp.Expression, config: GuardrailConfig) -> list[Violation]:
    depth = max((_select_depth(s) for s in tree.find_all(exp.Select)), default=0)
    if depth <= config.max_subquery_depth:
        return []
    return _block(
        "subquery_depth",
        f"subqueries nested {depth} levels deep (max {config.max_subquery_depth})",
    )


def _enforce_limit(tree: exp.Expression, max_rows: int) -> exp.Expression:
    """Add LIMIT max_rows, or lower an existing limit that is larger or not a literal."""
    limit = tree.args.get("limit")
    value = limit.expression if isinstance(limit, exp.Limit) else None
    if isinstance(value, exp.Literal) and value.is_int and int(value.this) <= max_rows:
        return tree
    return tree.limit(max_rows, copy=False)  # type: ignore[attr-defined]
