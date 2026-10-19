"""Database helpers (psycopg 3, async connection pool). All SQL uses bound parameters => no SQL injection."""
from pathlib import Path

from psycopg_pool import AsyncConnectionPool

SCHEMA = (Path(__file__).parent / "schema.sql").read_text()


async def migrate(pool: AsyncConnectionPool) -> None:
    """Apply schema.sql. The advisory lock stops several containers (api + seed)
    from migrating at the same time. It is released automatically on commit.
    For real production use a versioned tool (Alembic, Atlas)."""
    async with pool.connection() as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(727274)")
        await conn.execute(SCHEMA)


async def fetch_all(pool: AsyncConnectionPool, sql: str, params=None) -> list[dict]:
    async with pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()


async def fetch_one(pool: AsyncConnectionPool, sql: str, params=None) -> dict | None:
    async with pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchone()
