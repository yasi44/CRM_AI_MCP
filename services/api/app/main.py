"""CRM API (FastAPI). The ONLY component that touches the database and the ONLY place where
authorization is enforced. The MCP server and the AI agent are clients of this API, so an AI
can never do more than the human user whose token it carries."""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import get_settings
from .db import migrate
from .middleware import audit_mw, rate_limit_mw
from .routes import router

logging.basicConfig(level=logging.INFO, format='{"level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}')


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()  # fails fast on bad config
    pool = AsyncConnectionPool(s.database_url, min_size=1, max_size=10, open=False, kwargs={"row_factory": dict_row})
    await pool.open(wait=True, timeout=30)
    await migrate(pool)
    app.state.pool = pool
    yield
    await pool.close()  # graceful shutdown


docs = os.environ.get("DISABLE_DOCS") != "true"  # FastAPI auto-generates OpenAPI docs at /docs
app = FastAPI(title="CRM API", lifespan=lifespan, docs_url="/docs" if docs else None,
              redoc_url=None, openapi_url="/openapi.json" if docs else None)
app.include_router(router)

# Starlette runs the LAST registered middleware first, so order is: audit -> rate limit -> routes.
app.middleware("http")(rate_limit_mw)
app.middleware("http")(audit_mw)
