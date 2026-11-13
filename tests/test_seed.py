"""Unit tests for the synthetic data generator (no database needed)."""

from collections import Counter
from decimal import Decimal

import pytest

from text2sql.db.seed import PERIOD_END, PERIOD_START, SeedConfig, Table, generate


@pytest.fixture(scope="module")
def data() -> dict[str, Table]:
    return generate(SeedConfig.small())


def _rows(data: dict[str, Table], name: str) -> list[dict]:
    table = data[name]
    return [dict(zip(table.columns, row, strict=True)) for row in table.rows]


def test_generation_is_deterministic() -> None:
    a, b = generate(SeedConfig.small()), generate(SeedConfig.small())
    assert all(a[t].rows == b[t].rows for t in a)


def test_different_seed_gives_different_data() -> None:
    a = generate(SeedConfig.small())
    b = generate(SeedConfig(**{**SeedConfig.small().__dict__, "seed": 7}))
    assert a["orders"].rows != b["orders"].rows


def test_row_counts_match_config(data: dict[str, Table]) -> None:
    cfg = SeedConfig.small()
    assert len(data["customers"].rows) == cfg.n_customers
    assert len(data["products"].rows) == cfg.n_products
    assert len(data["orders"].rows) == cfg.n_orders
    assert len(data["stores"].rows) == cfg.n_physical_stores + 3
    assert len(data["order_items"].rows) >= cfg.n_orders  # at least one line per order


def test_foreign_keys_are_consistent(data: dict[str, Table]) -> None:
    ids = {t: {r[0] for r in data[t].rows} for t in ("customers", "stores", "products", "orders")}
    category_ids = {r[0] for r in data["categories"].rows}
    assert all(p["category_id"] in category_ids for p in _rows(data, "products"))
    for o in _rows(data, "orders"):
        assert o["customer_id"] in ids["customers"]
        assert o["store_id"] in ids["stores"]
    for i in _rows(data, "order_items"):
        assert i["order_id"] in ids["orders"]
        assert i["product_id"] in ids["products"]
    assert {i["order_id"] for i in _rows(data, "order_items")} == ids["orders"]


def test_primary_and_unique_keys(data: dict[str, Table]) -> None:
    for name in ("categories", "products", "stores", "customers", "orders", "order_items"):
        pks = [r[0] for r in data[name].rows]
        assert len(pks) == len(set(pks)), name
    assert len({c["email"] for c in _rows(data, "customers")}) == len(data["customers"].rows)
    lines = [(i["order_id"], i["product_id"]) for i in _rows(data, "order_items")]
    assert len(lines) == len(set(lines))
    inv = [(r[0], r[1]) for r in data["inventory"].rows]
    assert len(inv) == len(set(inv))


def test_orders_respect_business_rules(data: dict[str, Table]) -> None:
    customers = {c["customer_id"]: c for c in _rows(data, "customers")}
    stores = {s["store_id"]: s for s in _rows(data, "stores")}
    for o in _rows(data, "orders"):
        day = o["order_date"].date()
        assert PERIOD_START <= day <= PERIOD_END
        assert day >= customers[o["customer_id"]]["signup_date"]
        store = stores[o["store_id"]]
        assert day >= store["opened_on"]
        if store["store_type"] == "online":
            assert o["payment_method"] != "cash"
        else:
            assert o["shipped_date"] is None and o["payment_method"] != "paypal"
        if o["status"] in ("cancelled", "pending"):
            assert o["shipped_date"] is None
        if o["shipped_date"] is not None:
            assert o["shipped_date"] >= day
        if o["status"] == "pending":
            assert (PERIOD_END - day).days < 7


def test_order_lines_are_valid(data: dict[str, Table]) -> None:
    products = {p["product_id"]: p for p in _rows(data, "products")}
    orders = {o["order_id"]: o for o in _rows(data, "orders")}
    for i in _rows(data, "order_items"):
        assert i["quantity"] > 0
        assert Decimal("0") <= i["discount"] <= Decimal("0.5")
        assert i["unit_price"] > 0
        assert products[i["product_id"]]["created_at"] <= orders[i["order_id"]]["order_date"].date()


def test_distributions_are_realistic(data: dict[str, Table]) -> None:
    statuses = Counter(o["status"] for o in _rows(data, "orders"))
    assert statuses["completed"] / sum(statuses.values()) > 0.75
    tiers = {c["loyalty_tier"] for c in _rows(data, "customers")}
    assert {"bronze", "silver"} <= tiers
    online_store_ids = {s["store_id"] for s in _rows(data, "stores") if s["store_type"] == "online"}
    for s in _rows(data, "stores"):
        if s["store_id"] in online_store_ids:
            assert s["city"] is None and s["region"] is None
