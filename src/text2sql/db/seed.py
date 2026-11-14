"""Deterministic synthetic retail dataset: generation (pure Python) and loading (COPY).

Usage:
    python -m text2sql.db.seed            # full dataset (~40k orders, ~100k order lines)
    python -m text2sql.db.seed --small    # tiny dataset for quick checks
"""

from __future__ import annotations

import argparse
import logging
import random
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from importlib import resources
from typing import Any

import psycopg
from faker import Faker
from psycopg import sql

from text2sql.config import get_settings
from text2sql.db.engine import conninfo as _conninfo
from text2sql.db.roles import setup_readonly_role

logger = logging.getLogger(__name__)

PERIOD_START = date(2024, 1, 1)
PERIOD_END = date(2025, 12, 31)

# Region -> cities, with a rough population weight.
REGIONS: dict[str, tuple[float, tuple[str, ...]]] = {
    "Île-de-France": (0.25, ("Paris", "Boulogne-Billancourt", "Versailles", "Saint-Denis")),
    "Auvergne-Rhône-Alpes": (0.13, ("Lyon", "Grenoble", "Clermont-Ferrand", "Annecy")),
    "Provence-Alpes-Côte d'Azur": (0.10, ("Marseille", "Nice", "Toulon", "Aix-en-Provence")),
    "Occitanie": (0.10, ("Toulouse", "Montpellier", "Nîmes", "Perpignan")),
    "Nouvelle-Aquitaine": (0.10, ("Bordeaux", "Limoges", "Poitiers", "La Rochelle")),
    "Hauts-de-France": (0.09, ("Lille", "Amiens", "Roubaix", "Dunkerque")),
    "Grand Est": (0.08, ("Strasbourg", "Reims", "Metz", "Nancy")),
    "Pays de la Loire": (0.06, ("Nantes", "Angers", "Le Mans")),
    "Bretagne": (0.05, ("Rennes", "Brest", "Quimper")),
    "Normandie": (0.04, ("Rouen", "Caen", "Le Havre")),
}


@dataclass(frozen=True)
class CategorySpec:
    """Static description of a category used to generate plausible products."""

    name: str
    description: str
    nouns: tuple[str, ...]
    brands: tuple[str, ...]
    price_range: tuple[float, float]


CATEGORIES: tuple[CategorySpec, ...] = (
    CategorySpec("Electronics", "Audio, wearables and accessories",
                 ("Headphones", "Speaker", "Smartwatch", "Tablet", "Charger", "Keyboard"),
                 ("Voltix", "Sonara", "Nexio"), (15, 600)),
    CategorySpec("Home Appliances", "Small kitchen and home appliances",
                 ("Blender", "Kettle", "Toaster", "Vacuum Cleaner", "Coffee Maker"),
                 ("Maisona", "Kuchi", "Brevo"), (20, 400)),
    CategorySpec("Furniture", "Indoor furniture and lighting",
                 ("Chair", "Desk", "Bookshelf", "Lamp", "Side Table"),
                 ("Boisé", "Nordlund"), (30, 800)),
    CategorySpec("Clothing - Men", "Men's apparel",
                 ("T-Shirt", "Jeans", "Jacket", "Sweater", "Shirt"),
                 ("Urbain", "Atelier 9"), (10, 180)),
    CategorySpec("Clothing - Women", "Women's apparel",
                 ("Dress", "Blouse", "Skirt", "Coat", "Cardigan"),
                 ("Atelier 9", "Lumière"), (12, 220)),
    CategorySpec("Shoes", "Footwear for all",
                 ("Sneakers", "Boots", "Sandals", "Loafers"),
                 ("Pas Léger", "Stride"), (25, 200)),
    CategorySpec("Sports & Outdoors", "Sport, camping and cycling gear",
                 ("Yoga Mat", "Tent", "Backpack", "Bike Helmet", "Water Bottle"),
                 ("Altitude", "Vélo+"), (8, 350)),
    CategorySpec("Beauty", "Cosmetics and personal care",
                 ("Face Cream", "Shampoo", "Perfume", "Lipstick"),
                 ("Éclat", "Pure"), (5, 120)),
    CategorySpec("Grocery", "Fine food and beverages",
                 ("Coffee Beans", "Olive Oil", "Chocolate", "Tea Box", "Honey"),
                 ("Terroir", "Bon Goût"), (2, 40)),
    CategorySpec("Toys", "Toys and games",
                 ("Puzzle", "Board Game", "Building Set", "Plush Toy"),
                 ("Joujou", "Kidoo"), (6, 120)),
    CategorySpec("Books", "Books and comics",
                 ("Novel", "Cookbook", "Comic", "Travel Guide"),
                 ("Éditions Clair", "Pages"), (5, 45)),
    CategorySpec("Garden", "Garden tools and outdoor furniture",
                 ("Plant Pot", "Garden Hose", "Pruning Shears", "Lawn Chair"),
                 ("Vert", "Jardinor"), (6, 250)),
)  # fmt: skip

ADJECTIVES = ("Classic", "Pro", "Compact", "Premium", "Eco", "Ultra", "Essential", "Deluxe")
ONLINE_STORES = ("Online Store", "Mobile App", "Marketplace")
# Month weights: sales peak in November (Black Friday) and December (Christmas).
MONTH_WEIGHTS = {1: 0.85, 2: 0.8, 3: 0.9, 4: 0.95, 5: 1.0, 6: 1.05,
                 7: 1.0, 8: 0.85, 9: 0.95, 10: 1.0, 11: 1.45, 12: 1.6}  # fmt: skip
LOYALTY_THRESHOLDS = ((3000, "platinum"), (1000, "gold"), (200, "silver"), (0, "bronze"))


@dataclass(frozen=True)
class SeedConfig:
    """Size and randomness parameters of the generated dataset."""

    seed: int = 42
    n_customers: int = 5000
    n_products: int = 500
    n_physical_stores: int = 27
    n_orders: int = 40000

    @classmethod
    def small(cls) -> SeedConfig:
        return cls(n_customers=200, n_products=60, n_physical_stores=6, n_orders=800)


@dataclass
class Table:
    """Rows for one table, in column order (ready for COPY)."""

    name: str
    columns: tuple[str, ...]
    rows: list[tuple[Any, ...]] = field(default_factory=list)


def _money(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _random_date(rng: random.Random, start: date, end: date) -> date:
    return start + timedelta(days=rng.randint(0, (end - start).days))


def _ascii(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return "".join(c for c in normalized.lower() if c.isalnum())


def generate(config: SeedConfig) -> dict[str, Table]:
    """Generate the whole dataset deterministically from `config.seed`."""
    rng = random.Random(config.seed)  # noqa: S311 -- reproducible fake data, not crypto
    fake = Faker("fr_FR")
    fake.seed_instance(config.seed)

    tables = {
        "categories": Table("categories", ("category_id", "name", "description")),
        "products": Table("products", ("product_id", "sku", "name", "category_id", "brand",
                                       "unit_price", "unit_cost", "is_active", "created_at")),
        "stores": Table("stores", ("store_id", "name", "store_type", "city", "region",
                                   "opened_on")),
        "customers": Table("customers", ("customer_id", "first_name", "last_name", "email",
                                         "city", "region", "birth_date", "signup_date",
                                         "loyalty_tier")),
        "orders": Table("orders", ("order_id", "customer_id", "store_id", "order_date",
                                   "shipped_date", "status", "payment_method")),
        "order_items": Table("order_items", ("order_item_id", "order_id", "product_id",
                                             "quantity", "unit_price", "discount")),
        "inventory": Table("inventory", ("store_id", "product_id", "quantity_on_hand",
                                         "reorder_level", "last_restocked")),
    }  # fmt: skip

    # --- categories & products ---
    for cat_id, spec in enumerate(CATEGORIES, start=1):
        tables["categories"].rows.append((cat_id, spec.name, spec.description))

    product_created: dict[int, date] = {}
    product_price: dict[int, Decimal] = {}
    for pid in range(1, config.n_products + 1):
        cat_id = rng.randint(1, len(CATEGORIES))
        spec = CATEGORIES[cat_id - 1]
        low, high = spec.price_range
        price = _money(low * (high / low) ** rng.random())  # log-uniform: many cheap items
        cost = _money(float(price) * rng.uniform(0.35, 0.75))
        # 85% of the catalog predates the data period; the rest is launched during it.
        if rng.random() < 0.85:
            created = _random_date(rng, date(2022, 1, 1), date(2023, 12, 31))
        else:
            created = _random_date(rng, PERIOD_START, date(2025, 9, 30))
        brand = rng.choice(spec.brands)
        name = f"{brand} {rng.choice(ADJECTIVES)} {rng.choice(spec.nouns)}"
        tables["products"].rows.append(
            (
                pid,
                f"SKU-{pid:05d}",
                name,
                cat_id,
                brand,
                price,
                cost,
                rng.random() < 0.95,
                created,
            )
        )
        product_created[pid] = created
        product_price[pid] = price

    # --- stores ---
    region_names = list(REGIONS)
    region_weights = [REGIONS[r][0] for r in region_names]
    store_rows = tables["stores"].rows
    physical_by_region: dict[str, list[int]] = defaultdict(list)
    store_opened: dict[int, date] = {}
    used_names: set[str] = set()
    for i in range(config.n_physical_stores):
        # Every region gets at least one store before random allocation.
        region = (region_names[i] if i < len(region_names)
                  else rng.choices(region_names, region_weights)[0])  # fmt: skip
        city = rng.choice(REGIONS[region][1])
        name, n = f"{city} Store", 2
        while name in used_names:
            name, n = f"{city} Store {n}", n + 1
        used_names.add(name)
        # A couple of stores open during the data period (sales start at opening).
        if i >= len(region_names) and rng.random() < 0.1:
            opened = _random_date(rng, date(2024, 3, 1), date(2025, 6, 30))
        else:
            opened = _random_date(rng, date(2012, 1, 1), date(2023, 6, 30))
        store_id = len(store_rows) + 1
        store_rows.append((store_id, name, "physical", city, region, opened))
        physical_by_region[region].append(store_id)
        store_opened[store_id] = opened
    online_ids = []
    for name in ONLINE_STORES:
        store_id = len(store_rows) + 1
        store_rows.append((store_id, name, "online", None, None, date(2020, 1, 1)))
        online_ids.append(store_id)
        store_opened[store_id] = date(2020, 1, 1)
    all_physical = [sid for ids in physical_by_region.values() for sid in ids]

    # --- customers ---
    customer_region: dict[int, str] = {}
    customer_signup: dict[int, date] = {}
    for cid in range(1, config.n_customers + 1):
        first, last = fake.first_name(), fake.last_name()
        region = rng.choices(region_names, region_weights)[0]
        city = rng.choice(REGIONS[region][1])
        # Most customers signed up before the period; some join during it.
        signup = _random_date(rng, date(2019, 1, 1), date(2025, 11, 30))
        email = f"{_ascii(first)}.{_ascii(last)}{cid}@example.com"
        birth = _random_date(rng, date(1945, 1, 1), date(2006, 12, 31))
        tables["customers"].rows.append(
            [cid, first, last, email, city, region, birth, signup, "bronze"]  # tier set below
        )
        customer_region[cid] = region
        customer_signup[cid] = signup

    # --- orders & order lines ---
    days = [PERIOD_START + timedelta(d) for d in range((PERIOD_END - PERIOD_START).days + 1)]
    day_weights = [
        MONTH_WEIGHTS[d.month]
        * (1.25 if d.weekday() >= 5 else 1.0)  # weekend
        * (1.1 if d.year == 2025 else 1.0)  # yearly growth
        for d in days
    ]
    # Pareto weights: a few customers and products concentrate most of the activity.
    customer_weights = [rng.paretovariate(1.5) for _ in range(config.n_customers)]
    product_ids = list(range(1, config.n_products + 1))
    product_weights = [rng.paretovariate(1.2) for _ in product_ids]
    customer_ids = rng.choices(range(1, config.n_customers + 1), customer_weights,
                               k=config.n_orders)  # fmt: skip
    order_days = rng.choices(days, day_weights, k=config.n_orders)

    lifetime_spend: dict[int, Decimal] = defaultdict(Decimal)
    item_id = 0
    for oid, (cid, day) in enumerate(zip(customer_ids, order_days, strict=True), start=1):
        if day < customer_signup[cid]:  # no order before signup
            day = _random_date(rng, customer_signup[cid], PERIOD_END)

        online = rng.random() < 0.3
        if online:
            store_id = rng.choice(online_ids)
            hour = rng.choices(range(24), [1] * 7 + [3] * 11 + [5] * 6)[0]
        else:
            local = [s for s in physical_by_region[customer_region[cid]]
                     if store_opened[s] <= day]  # fmt: skip
            candidates = local or [s for s in all_physical if store_opened[s] <= day]
            store_id = rng.choice(candidates)
            hour = rng.randint(9, 19)
        order_ts = datetime.combine(day, time(hour, rng.randint(0, 59), rng.randint(0, 59)))

        if (PERIOD_END - day).days < 7:
            status = rng.choices(("pending", "completed", "cancelled"), (60, 35, 5))[0]
        else:
            status = rng.choices(("completed", "cancelled", "returned"), (88, 6, 6))[0]

        shipped = None
        if online and status in ("completed", "returned"):
            shipped = day + timedelta(days=rng.randint(1, 5))

        if online:
            payment = rng.choices(("card", "paypal", "gift_card"), (75, 20, 5))[0]
        else:
            payment = rng.choices(("card", "cash", "gift_card"), (70, 20, 10))[0]

        tables["orders"].rows.append((oid, cid, store_id, order_ts, shipped, status, payment))

        n_lines = rng.choices((1, 2, 3, 4, 5, 6), (35, 28, 17, 10, 6, 4))[0]
        chosen: set[int] = set()
        for _ in range(n_lines * 5):  # rejection sampling: product must exist at order time
            if len(chosen) == n_lines:
                break
            pid = rng.choices(product_ids, product_weights)[0]
            if pid not in chosen and product_created[pid] <= day:
                chosen.add(pid)
        if not chosen:  # every order has at least one line
            chosen.add(rng.choice([p for p in product_ids if product_created[p] <= day]))
        for pid in sorted(chosen):
            item_id += 1
            # 2024 prices were ~4% lower than the current list price.
            price = (
                _money(product_price[pid] * Decimal("0.96"))
                if day.year == 2024
                else product_price[pid]
            )
            promo_rate = 0.5 if day.month == 11 else 0.25
            discount = (Decimal(str(rng.choice((0.05, 0.10, 0.15, 0.20, 0.30))))
                        if rng.random() < promo_rate else Decimal("0"))  # fmt: skip
            qty = rng.choices((1, 2, 3, 4, 5), (70, 18, 7, 3, 2))[0]
            tables["order_items"].rows.append((item_id, oid, pid, qty, price, discount))
            if status == "completed":
                lifetime_spend[cid] += qty * price * (1 - discount)

    # --- loyalty tier derived from actual spend (keeps data internally consistent) ---
    for row in tables["customers"].rows:
        spend = lifetime_spend[row[0]]
        row[8] = next(tier for threshold, tier in LOYALTY_THRESHOLDS if spend >= threshold)
    tables["customers"].rows = [tuple(r) for r in tables["customers"].rows]

    # --- inventory snapshot (physical stores only) ---
    for store_id in all_physical:
        for pid in product_ids:
            qty = 0 if rng.random() < 0.05 else rng.randint(1, 120)
            tables["inventory"].rows.append(
                (
                    store_id,
                    pid,
                    qty,
                    rng.randint(5, 20),
                    _random_date(rng, PERIOD_END - timedelta(days=60), PERIOD_END),
                )
            )

    return tables


def load(database_url: str, tables: dict[str, Table]) -> None:
    """(Re)create the schema and bulk-load all tables with COPY, in one transaction."""
    schema_sql = resources.files("text2sql.db").joinpath("schema.sql").read_text("utf-8")
    with psycopg.connect(_conninfo(database_url)) as conn:
        conn.execute(schema_sql)  # type: ignore[arg-type]  # trusted static file
        for table in tables.values():
            copy_stmt = sql.SQL("COPY {} ({}) FROM STDIN").format(
                sql.Identifier(table.name),
                sql.SQL(", ").join(map(sql.Identifier, table.columns)),
            )
            with conn.cursor().copy(copy_stmt) as copy:
                for row in table.rows:
                    copy.write_row(row)
            logger.info("loaded table=%s rows=%d", table.name, len(table.rows))
            # Explicit IDs were inserted: move identity sequences past them.
            id_col = table.columns[0]
            if id_col.endswith("_id") and table.name != "inventory":
                conn.execute(
                    sql.SQL(
                        "SELECT setval(pg_get_serial_sequence({tbl}, {col}), "
                        "(SELECT COALESCE(MAX({ident}), 1) FROM {table}))"
                    ).format(
                        tbl=sql.Literal(table.name),
                        col=sql.Literal(id_col),
                        ident=sql.Identifier(id_col),
                        table=sql.Identifier(table.name),
                    )
                )
    # ANALYZE outside the transaction: fresh statistics make EXPLAIN cost estimates meaningful.
    with psycopg.connect(_conninfo(database_url), autocommit=True) as conn:
        conn.execute("ANALYZE")


def _has_data(database_url: str) -> bool:
    with psycopg.connect(_conninfo(database_url)) as conn:
        exists = conn.execute("SELECT to_regclass('public.orders')").fetchone()
        if not exists or exists[0] is None:
            return False
        row = conn.execute("SELECT EXISTS (SELECT 1 FROM orders)").fetchone()
        return bool(row and row[0])


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the demo retail database.")
    parser.add_argument("--small", action="store_true", help="generate a tiny dataset")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--if-empty",
        action="store_true",
        help="load data only if the database has no orders yet (docker compose)",
    )
    args = parser.parse_args()

    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s %(message)s")
    base = SeedConfig.small() if args.small else SeedConfig()
    config = SeedConfig(**{**base.__dict__, "seed": args.seed})

    if args.if_empty and _has_data(settings.database_url):
        logger.info("data already loaded: skipping generation (roles and app schema refreshed)")
    else:
        tables = generate(config)
        load(settings.database_url, tables)
        logger.info("seed complete: %s", {t.name: len(t.rows) for t in tables.values()})
    from text2sql.history import ensure_app_schema  # history/feedback tables, kept on reseed

    ensure_app_schema(settings.database_url)
    if settings.readonly_database_url:
        setup_readonly_role(settings.database_url, settings.readonly_database_url)
    else:
        logger.warning("READONLY_DATABASE_URL not set: read-only role not created")


if __name__ == "__main__":
    main()
