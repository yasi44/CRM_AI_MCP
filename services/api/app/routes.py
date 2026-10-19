"""REST routes. Each route declares the permission it needs via Depends(require(...))."""
import asyncio
import logging
import secrets
from typing import Literal

import bcrypt
import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from psycopg.errors import ForeignKeyViolation
from pydantic import BaseModel, ConfigDict, Field

from .config import get_settings
from .db import fetch_all, fetch_one
from .security import DUMMY_HASH, Principal, check_password, has_perm, issue_token, require

log = logging.getLogger("api")
router = APIRouter()
Stage = Literal["Prospecting", "Engaging", "Won", "Lost"]  # invalid values => automatic 422


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown JSON fields are rejected


# ---------------------------------------------------------------- health ----
@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request):
    try:
        await fetch_one(request.app.state.pool, "SELECT 1")
    except Exception:
        raise HTTPException(503, "db unavailable")
    return {"status": "ready"}


# ------------------------------------------------------------------ auth ----
class LoginIn(Strict):
    email: str = Field(max_length=200)
    password: str = Field(max_length=200)


@router.post("/auth/login")
async def login(body: LoginIn, request: Request):
    row = await fetch_one(request.app.state.pool,
                          "SELECT id::text AS id, password_hash, role FROM users WHERE email=%s",
                          (body.email.strip().lower(),))
    stored = row["password_hash"].encode() if row else DUMMY_HASH
    # bcrypt is CPU-heavy on purpose: run it in a thread so the event loop stays responsive.
    ok = await asyncio.to_thread(check_password, body.password, stored)
    if not row or not ok:
        raise HTTPException(401, "invalid credentials")  # same message for both failure modes
    request.state.audit.update(user_id=row["id"], email=body.email, role=row["role"])
    s = get_settings()
    return {"access_token": issue_token(row["id"], body.email, row["role"]),
            "token_type": "Bearer", "expires_in": s.token_ttl_seconds}


@router.get("/v1/me")
async def me(p: Principal = Depends(require())):
    return {"ID": p.id, "Email": p.email, "Role": p.role}


# -------------------------------------------------------------- accounts ----
ACCOUNT_COLS = "id, name, sector, year_established, revenue_musd::float8 AS revenue_musd, employees, office_location, subsidiary_of"


def redact_account(a: dict, p: Principal) -> dict:
    if not has_perm(p.role, "revenue:read"):
        a.pop("revenue_musd", None)  # field-level privacy control
    return a


@router.get("/v1/accounts")
async def list_accounts(request: Request, q: str = "", limit: int = Query(25, ge=1, le=100),
                        p: Principal = Depends(require("accounts:read"))):
    rows = await fetch_all(request.app.state.pool,
                           f"SELECT {ACCOUNT_COLS} FROM accounts WHERE name ILIKE %s ORDER BY name LIMIT %s",
                           (f"%{q}%", limit))  # bound parameters => no SQL injection
    return {"accounts": [redact_account(r, p) for r in rows]}


@router.get("/v1/accounts/{account_id}")
async def get_account(account_id: int, request: Request, p: Principal = Depends(require("accounts:read"))):
    row = await fetch_one(request.app.state.pool, f"SELECT {ACCOUNT_COLS} FROM accounts WHERE id=%s", (account_id,))
    if not row:
        raise HTTPException(404, "account not found")
    return redact_account(row, p)


# --------------------------------------------------------- opportunities ----
OPP_SELECT = """SELECT o.id, o.agent, o.product, a.name AS account, o.stage,
       to_char(o.engage_date,'YYYY-MM-DD') AS engage_date, to_char(o.close_date,'YYYY-MM-DD') AS close_date,
       o.close_value::float8 AS close_value
  FROM opportunities o LEFT JOIN accounts a ON a.id = o.account_id"""


@router.get("/v1/opportunities")
async def list_opportunities(request: Request, stage: Stage | None = None, agent: str | None = None,
                             limit: int = Query(25, ge=1, le=100),
                             p: Principal = Depends(require("opportunities:read"))):
    rows = await fetch_all(
        request.app.state.pool,
        OPP_SELECT + """ WHERE (%(stage)s::text IS NULL OR o.stage = %(stage)s)
                           AND (%(agent)s::text IS NULL OR o.agent = %(agent)s)
                         ORDER BY o.engage_date DESC NULLS LAST, o.id LIMIT %(limit)s""",
        {"stage": stage, "agent": agent, "limit": limit})
    if not has_perm(p.role, "deal_value:read"):
        for r in rows:
            r.pop("close_value", None)
    return {"opportunities": rows}


class OppIn(Strict):
    agent: str = Field(min_length=1, max_length=100)
    product: str = Field(min_length=1, max_length=100)
    account_id: int | None = None
    stage: Stage = "Prospecting"


@router.post("/v1/opportunities", status_code=201)
async def create_opportunity(body: OppIn, request: Request, p: Principal = Depends(require("opportunities:write"))):
    new_id = "OPP-" + secrets.token_hex(4)
    try:
        async with request.app.state.pool.connection() as conn:
            await conn.execute(
                """INSERT INTO opportunities (id,agent,product,account_id,stage,engage_date)
                   VALUES (%s,%s,%s,%s,%s,CURRENT_DATE)""",
                (new_id, body.agent, body.product, body.account_id, body.stage))
    except ForeignKeyViolation:
        raise HTTPException(422, "unknown agent, product or account")
    request.state.audit["detail"] = {"opportunity_id": new_id, "action": "create"}
    return {"id": new_id}


class StageIn(Strict):
    stage: Stage
    close_value: float | None = Field(default=None, ge=0)


async def notify_won(opp_id: str) -> None:
    """Third-party integration: Slack-compatible webhook. Runs AFTER the response is sent,
    with a hard timeout, so a slow third party can never slow down or break our API."""
    url = get_settings().slack_webhook
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(url, json={"text": f"Deal {opp_id} was marked Won"})
    except Exception as e:
        log.warning("webhook failed: %s", e)


@router.patch("/v1/opportunities/{opp_id}/stage")
async def update_stage(opp_id: str, body: StageIn, request: Request, tasks: BackgroundTasks,
                       p: Principal = Depends(require("opportunities:write"))):
    # close_value is money: changing it additionally requires deal_value:read.
    if body.close_value is not None and not has_perm(p.role, "deal_value:read"):
        raise HTTPException(403, "forbidden: close_value requires deal_value:read")
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute(
            """UPDATE opportunities
               SET stage=%(stage)s::text,
                   close_value=COALESCE(%(cv)s::numeric, close_value),
                   close_date=CASE WHEN %(stage)s::text IN ('Won','Lost') THEN CURRENT_DATE ELSE close_date END,
                   updated_at=now()
               WHERE id=%(id)s""",
            {"stage": body.stage, "cv": body.close_value, "id": opp_id})
        if cur.rowcount == 0:
            raise HTTPException(404, "opportunity not found")
    request.state.audit["detail"] = {"opportunity_id": opp_id, "action": "stage_change", "new_stage": body.stage}
    if body.stage == "Won":
        tasks.add_task(notify_won, opp_id)
    return {"id": opp_id, "stage": body.stage}


# ------------------------------------------------------------- analytics ----
@router.get("/v1/analytics/pipeline")
async def pipeline(request: Request, p: Principal = Depends(require("analytics:read"))):
    rows = await fetch_all(request.app.state.pool,
                           """SELECT stage, count(*)::int AS count, COALESCE(sum(close_value),0)::float8 AS total_value
                              FROM opportunities GROUP BY stage ORDER BY stage""")
    counts = {r["stage"]: r["count"] for r in rows}
    if not has_perm(p.role, "deal_value:read"):
        for r in rows:
            r.pop("total_value")
    won, lost = counts.get("Won", 0), counts.get("Lost", 0)
    return {"stages": rows, "win_rate": won / (won + lost) if won + lost else 0.0}


# ----------------------------------------------------------------- audit ----
@router.get("/v1/audit")
async def list_audit(request: Request, email: str | None = None, limit: int = Query(50, ge=1, le=500),
                     p: Principal = Depends(require("audit:read"))):
    rows = await fetch_all(
        request.app.state.pool,
        """SELECT id, ts, COALESCE(request_id,'') AS request_id, COALESCE(email,'') AS email, COALESCE(role,'') AS role,
                  method, path, status, COALESCE(ip,'') AS ip, detail
           FROM audit_log WHERE (%(email)s::text IS NULL OR email = %(email)s) ORDER BY id DESC LIMIT %(limit)s""",
        {"email": email, "limit": limit})
    return {"entries": rows}
