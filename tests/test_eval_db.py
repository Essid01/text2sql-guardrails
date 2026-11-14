"""Every reference query of the benchmark runs on PostgreSQL as the read-only role."""

import pytest

from text2sql.eval.dataset import EvalCase, load_cases
from text2sql.execution import QueryExecutor
from text2sql.guardrails import GuardrailConfig, check_sql

pytestmark = pytest.mark.integration
TABLES = frozenset(
    {"categories", "products", "stores", "customers", "orders", "order_items", "inventory"}
)


@pytest.mark.parametrize(
    "case", [c for c in load_cases() if c.expect == "answer"], ids=lambda c: c.id
)
def test_reference_sql_executes(readonly_url: str, case: EvalCase) -> None:
    executor = QueryExecutor(readonly_url)
    for sql in [*case.golds, *([case.bad_sql] if case.bad_sql else [])]:
        guarded = check_sql(sql, GuardrailConfig(allowed_tables=TABLES))
        assert guarded.sql is not None
        result = executor.execute(guarded.sql)
        assert not result.truncated
