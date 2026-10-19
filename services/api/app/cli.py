"""Command line tasks:  python -m app.cli  seed | migrate | create-user EMAIL PASSWORD ROLE | healthcheck"""
import asyncio
import csv
import logging
import os
import sys
import urllib.request

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import get_settings
from .db import migrate
from .security import ROLE_PERMS, hash_password

log = logging.getLogger("cli")


def read_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]


async def create_user(pool, email: str, password: str, role: str) -> None:
    if len(password) < 12:
        raise SystemExit("password must be at least 12 characters")
    if role not in ROLE_PERMS:
        raise SystemExit(f"unknown role {role!r}")
    async with pool.connection() as conn:
        await conn.execute(
            """INSERT INTO users (email,password_hash,role) VALUES (%s,%s,%s)
               ON CONFLICT (email) DO UPDATE SET password_hash=EXCLUDED.password_hash, role=EXCLUDED.role""",
            (email.lower(), hash_password(password), role))
    log.info("user ready: %s (%s)", email, role)


async def seed(pool, data_dir: str) -> None:
    """Loads the 4 CSVs of the 'CRM Sales Opportunities' dataset. Safe to re-run (upserts).
    Values are passed as text; NULLIF(x,'')::type turns empty CSV cells into proper NULLs."""
    r = lambda f: read_csv(os.path.join(data_dir, f))  # noqa: E731
    accounts, products, teams, opps = r("accounts.csv"), r("products.csv"), r("sales_teams.csv"), r("sales_pipeline.csv")
    async with pool.connection() as conn:  # one transaction: all or nothing
        for a in accounts:
            await conn.execute(
                """INSERT INTO accounts (name,sector,year_established,revenue_musd,employees,office_location,subsidiary_of)
                   VALUES (%s,NULLIF(%s,''),NULLIF(%s,'')::int,NULLIF(%s,'')::numeric,NULLIF(%s,'')::int,NULLIF(%s,''),NULLIF(%s,''))
                   ON CONFLICT (name) DO UPDATE SET sector=EXCLUDED.sector, year_established=EXCLUDED.year_established,
                     revenue_musd=EXCLUDED.revenue_musd, employees=EXCLUDED.employees,
                     office_location=EXCLUDED.office_location, subsidiary_of=EXCLUDED.subsidiary_of""",
                (a["account"], a.get("sector"), a.get("year_established"), a.get("revenue"), a.get("employees"),
                 a.get("office_location"), a.get("subsidiary_of")))
        for p in products:
            await conn.execute(
                """INSERT INTO products (name,series,sales_price) VALUES (%s,NULLIF(%s,''),NULLIF(%s,'')::numeric)
                   ON CONFLICT (name) DO UPDATE SET series=EXCLUDED.series, sales_price=EXCLUDED.sales_price""",
                (p["product"], p.get("series"), p.get("sales_price")))
        for t in teams:
            await conn.execute(
                """INSERT INTO sales_agents (name,manager,regional_office) VALUES (%s,NULLIF(%s,''),NULLIF(%s,''))
                   ON CONFLICT (name) DO UPDATE SET manager=EXCLUDED.manager, regional_office=EXCLUDED.regional_office""",
                (t["sales_agent"], t.get("manager"), t.get("regional_office")))
        for o in opps:
            # Real-world data is messy (the original dataset spells one product "GTXPro" in the pipeline but
            # "GTX Pro" in the product list). Create placeholders instead of crashing so the row still loads.
            await conn.execute("INSERT INTO products (name) VALUES (%s) ON CONFLICT DO NOTHING", (o["product"],))
            await conn.execute("INSERT INTO sales_agents (name) VALUES (%s) ON CONFLICT DO NOTHING", (o["sales_agent"],))
            await conn.execute(
                """INSERT INTO opportunities (id,agent,product,account_id,stage,engage_date,close_date,close_value)
                   VALUES (%s,%s,%s,(SELECT id FROM accounts WHERE name=%s),%s,NULLIF(%s,'')::date,NULLIF(%s,'')::date,NULLIF(%s,'')::numeric)
                   ON CONFLICT (id) DO UPDATE SET stage=EXCLUDED.stage, close_date=EXCLUDED.close_date, close_value=EXCLUDED.close_value""",
                (o["opportunity_id"], o["sales_agent"], o["product"], o.get("account"), o["deal_stage"],
                 o.get("engage_date"), o.get("close_date"), o.get("close_value")))
    log.info("seeded accounts=%d products=%d agents=%d opportunities=%d", len(accounts), len(products), len(teams), len(opps))

    # Demo users are OPT-IN. Never ship default passwords to production.
    if os.environ.get("SEED_DEMO_USERS") == "true":
        for email, role in [("viewer@demo.local", "viewer"), ("rep@demo.local", "rep"),
                            ("manager@demo.local", "manager"), ("admin@demo.local", "admin")]:
            await create_user(pool, email, os.environ.get("DEMO_PASSWORD", ""), role)


async def main(argv: list[str]) -> None:
    cmd = argv[0] if argv else ""
    if cmd == "healthcheck":  # used by the Docker HEALTHCHECK
        try:
            urllib.request.urlopen("http://127.0.0.1:8080/healthz", timeout=3)
        except Exception:
            sys.exit(1)
        return
    s = get_settings()
    pool = AsyncConnectionPool(s.database_url, min_size=1, max_size=3, open=False, kwargs={"row_factory": dict_row})
    await pool.open(wait=True, timeout=30)
    try:
        await migrate(pool)
        if cmd == "migrate":
            log.info("migrations applied")
        elif cmd == "seed":
            await seed(pool, s.data_dir)
        elif cmd == "create-user" and len(argv) == 4:
            await create_user(pool, *argv[1:])
        elif cmd not in ("migrate",):
            sys.exit(__doc__)
    finally:
        await pool.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(main(sys.argv[1:]))
