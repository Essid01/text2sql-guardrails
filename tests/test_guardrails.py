"""Guardrail tests, written before the implementation (security-first).

Each blocked case names the rule that must catch it: a query blocked for the wrong
reason means the right rule has a hole. Allowed cases guard against false positives,
which would make the system unusable.
"""

import pytest

from text2sql.guardrails import GuardrailConfig, check_sql

TABLES = frozenset(
    {"categories", "customers", "inventory", "order_items", "orders", "products", "stores"}
)
CONFIG = GuardrailConfig(allowed_tables=TABLES, max_rows=1000, max_subquery_depth=3)


def _rules(sql: str) -> set[str]:
    return {v.rule for v in check_sql(sql, CONFIG).violations}


# --------------------------------------------------------------------------- blocked

BLOCKED = [
    # --- plain writes and DDL (the statement itself is not a query)
    ("DROP TABLE customers", "not_select"),
    ("dRoP tAbLe customers", "not_select"),
    ("DELETE FROM orders", "not_select"),
    ("UPDATE orders SET status = 'completed'", "not_select"),
    ("INSERT INTO categories (name) VALUES ('x')", "not_select"),
    ("TRUNCATE orders", "not_select"),
    ("ALTER TABLE orders ADD COLUMN x int", "not_select"),
    ("CREATE TABLE x (id int)", "not_select"),
    ("GRANT ALL ON orders TO PUBLIC", "not_select"),
    ("MERGE INTO orders o USING stores s ON o.store_id = s.store_id "
     "WHEN MATCHED THEN DELETE", "not_select"),
    # --- non-query statements: session, transactions, procedural code, admin
    ("COPY orders TO '/tmp/orders.csv'", "not_select"),
    ("SET statement_timeout = 0", "not_select"),
    ("BEGIN", "not_select"),
    ("VACUUM orders", "not_select"),
    ("DO $$ BEGIN DELETE FROM orders; END $$", "not_select"),
    ("CALL refresh_everything()", "not_select"),
    ("PREPARE p AS DELETE FROM orders", "not_select"),
    ("EXPLAIN ANALYZE DELETE FROM orders", "not_select"),  # ANALYZE executes the DELETE
    ("LISTEN channel", "not_select"),
    # --- stacked statements, including hidden behind comments
    ("SELECT 1; DROP TABLE customers", "multiple_statements"),
    ("SELECT 1; -- harmless\nDROP TABLE customers", "multiple_statements"),
    ("SELECT 1;DELETE FROM orders;", "multiple_statements"),
    ("/* report */ DROP TABLE customers", "not_select"),
    # --- writes hidden inside a query
    ("WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d", "write_operation"),
    ("WITH u AS (UPDATE orders SET status = 'x' RETURNING order_id) SELECT * FROM u",
     "write_operation"),
    ("SELECT * INTO orders_backup FROM orders", "write_operation"),
    ("SELECT * FROM orders FOR UPDATE", "write_operation"),
    ("SELECT * FROM orders FOR SHARE", "write_operation"),
    # --- dangerous functions (file access, sleep/DoS, config, sequences, remote)
    ("SELECT pg_sleep(10)", "blocked_function"),
    ("SELECT PG_SLEEP(10)", "blocked_function"),
    ("SELECT pg_read_file('/etc/passwd')", "blocked_function"),
    ("SELECT lo_import('/etc/passwd')", "blocked_function"),
    ("SELECT set_config('statement_timeout', '0', false)", "blocked_function"),
    ("SELECT nextval('orders_order_id_seq')", "blocked_function"),  # writes a sequence
    ("SELECT pg_terminate_backend(123)", "blocked_function"),
    ("SELECT * FROM dblink('host=evil', 'SELECT 1') AS t(x int)", "blocked_function"),
    ("SELECT order_id FROM orders UNION SELECT pg_sleep(5)::int", "blocked_function"),
    ("SELECT (SELECT pg_read_file('x')) AS leak", "blocked_function"),
    ("SELECT current_setting('data_directory')", "blocked_function"),
    # --- tables outside the schema (system catalogs, unknown tables)
    ("SELECT * FROM pg_catalog.pg_user", "unknown_table"),
    ("SELECT * FROM pg_shadow", "unknown_table"),
    ("SELECT * FROM information_schema.tables", "unknown_table"),
    ("SELECT * FROM suppliers", "unknown_table"),
    ("SELECT * FROM public.orders o JOIN secret.salaries s ON true", "unknown_table"),
    # --- complexity
    ("SELECT * FROM orders WHERE customer_id IN (SELECT customer_id FROM customers WHERE "
     "customer_id IN (SELECT customer_id FROM orders WHERE store_id IN (SELECT store_id "
     "FROM stores WHERE store_id IN (SELECT store_id FROM inventory))))",
     "subquery_depth"),
    ("SELECT 1 " + " " * 10_001, "too_long"),
    # --- not SQL at all
    ("", "empty"),
    ("   \n  ", "empty"),
    ("-- only a comment", "empty"),
    ("SELEC * FROM orders", "parse_error"),
    ("SELECT * FROM orders WHERE (", "parse_error"),
]  # fmt: skip


@pytest.mark.parametrize(("sql", "rule"), BLOCKED, ids=[s[:50] for s, _ in BLOCKED])
def test_blocked(sql: str, rule: str) -> None:
    result = check_sql(sql, CONFIG)
    assert not result.allowed
    assert result.sql is None  # nothing executable is ever returned when blocked
    assert rule in {v.rule for v in result.violations}, result.violations


# --------------------------------------------------------------------------- allowed

ALLOWED = [
    "SELECT COUNT(*) FROM orders",
    "select count(*) from orders;",  # lowercase, single trailing semicolon
    "SELECT o.order_id FROM orders o WHERE o.status = 'cancelled'",
    # dangerous words inside string literals or comments are just data
    "SELECT c.name FROM categories c WHERE c.name = 'DROP TABLE customers'",
    "SELECT c.name FROM categories c WHERE c.description LIKE '%; DELETE FROM orders%'",
    "SELECT * FROM orders /* ; DROP TABLE customers */",
    "SELECT 1 AS x -- DROP TABLE customers",
    # identifiers that look like keywords
    'SELECT o.status AS "update", o.order_id AS delete_count FROM orders o',
    # joins, CTEs, set operations, windows, usual functions
    "SELECT c.region, COUNT(DISTINCT o.order_id) FROM customers c "
    "JOIN orders o ON o.customer_id = c.customer_id GROUP BY c.region",
    "WITH s AS (SELECT store_id, COUNT(*) AS n FROM orders GROUP BY store_id) "
    "SELECT st.name, s.n FROM s JOIN stores st ON st.store_id = s.store_id",
    "SELECT city FROM customers UNION SELECT city FROM stores",
    "SELECT order_id, RANK() OVER (PARTITION BY store_id ORDER BY order_date) FROM orders",
    "SELECT DATE_TRUNC('month', order_date)::date AS m, COUNT(*) FROM orders GROUP BY 1",
    "SELECT COALESCE(shipped_date, order_date::date), "
    "CASE WHEN status = 'completed' THEN 1 ELSE 0 END FROM orders",
    "SELECT ROUND(SUM(quantity * unit_price * (1 - discount)), 2) FROM order_items",
    "SELECT EXTRACT(YEAR FROM order_date) AS y FROM orders",
    "SELECT d::date FROM generate_series('2025-01-01'::date, '2025-12-31', '1 month') AS d",
    "SELECT * FROM public.orders",  # explicit default schema
    # nesting exactly at the limit (3)
    "SELECT * FROM orders WHERE customer_id IN (SELECT customer_id FROM customers WHERE "
    "customer_id IN (SELECT customer_id FROM orders WHERE store_id IN "
    "(SELECT store_id FROM stores)))",
]


@pytest.mark.parametrize("sql", ALLOWED, ids=[s[:50] for s in ALLOWED])
def test_allowed(sql: str) -> None:
    result = check_sql(sql, CONFIG)
    assert result.allowed, result.violations
    assert result.violations == []
    assert result.sql is not None


# --------------------------------------------------------------------------- rewriting


def _limit_of(sql: str) -> str:
    rewritten = check_sql(sql, CONFIG).sql
    assert rewritten is not None
    return rewritten.upper().rsplit("LIMIT", 1)[1].strip() if "LIMIT" in rewritten.upper() else ""


def test_limit_added_when_missing() -> None:
    assert _limit_of("SELECT * FROM orders") == "1000"


def test_small_limit_kept() -> None:
    assert _limit_of("SELECT * FROM orders LIMIT 50") == "50"


def test_large_limit_capped() -> None:
    assert _limit_of("SELECT * FROM orders LIMIT 5000") == "1000"


def test_non_literal_limit_replaced() -> None:
    assert _limit_of("SELECT * FROM orders LIMIT ALL") == "1000"


def test_limit_applies_to_whole_union() -> None:
    sql = check_sql("SELECT city FROM customers UNION SELECT city FROM stores", CONFIG).sql
    assert sql is not None and sql.upper().rstrip().endswith("LIMIT 1000")


def test_rewritten_sql_has_no_comments() -> None:
    """What runs is exactly what was checked: comments cannot smuggle anything."""
    sql = check_sql("SELECT 1 AS x /* hidden */ -- tail", CONFIG).sql
    assert sql is not None and "hidden" not in sql and "tail" not in sql


def test_violations_explain_the_reason() -> None:
    result = check_sql("SELECT * FROM suppliers", CONFIG)
    assert any("suppliers" in v.message for v in result.violations)


def test_custom_config_is_respected() -> None:
    strict = GuardrailConfig(allowed_tables=TABLES, max_rows=10, max_subquery_depth=0)
    assert not check_sql("SELECT * FROM (SELECT 1) t", strict).allowed
    assert check_sql("SELECT * FROM orders", strict).sql.upper().endswith("LIMIT 10")


def test_hand_written_examples_pass() -> None:
    """Realistic analytics SQL (the few-shot pool) must never be blocked."""
    from text2sql.prompt.builder import load_examples

    for example in load_examples():
        result = check_sql(example.sql, CONFIG)
        assert result.allowed, (example.id, result.violations)
