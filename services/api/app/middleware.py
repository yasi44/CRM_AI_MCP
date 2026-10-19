"""Cross-cutting concerns: request id, audit log, rate limiting."""
import logging
import secrets
import time

from fastapi import Request
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb

log = logging.getLogger("api")
SKIP_AUDIT = {"/healthz", "/readyz"}


def client_ip(request: Request) -> str:
    # NOTE: behind a load balancer, use its trusted X-Forwarded-For handling instead.
    return request.client.host if request.client else ""


class Limiter:
    """Fixed-window per-IP limiter. Fine for ONE replica; with many replicas use Redis or the gateway."""

    def __init__(self, limit: int, window: int):
        self.limit, self.window, self.hits = limit, window, {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        if len(self.hits) > 10_000:  # opportunistic cleanup keeps memory bounded
            self.hits = {k: v for k, v in self.hits.items() if v[1] > now}
        count, reset = self.hits.get(key, (0, now + self.window))
        if now > reset:
            count, reset = 0, now + self.window
        self.hits[key] = (count + 1, reset)
        return count + 1 <= self.limit


limiter = Limiter(limit=120, window=60)


async def rate_limit_mw(request: Request, call_next):
    if not limiter.allow(client_ip(request)):
        return JSONResponse({"error": "rate limit exceeded"}, status_code=429, headers={"Retry-After": "60"})
    return await call_next(request)


async def audit_mw(request: Request, call_next):
    """Records EVERY request (including failed logins, 401s and 403s) to an append-only table.
    Failed attempts are exactly what auditors want to see."""
    if request.url.path in SKIP_AUDIT:
        return await call_next(request)
    request_id = secrets.token_hex(8)
    request.state.audit = {"user_id": None, "email": None, "role": None, "detail": None}  # filled in later by auth/routes
    try:
        response = await call_next(request)
    except Exception:
        log.exception("unhandled error")  # real error goes to logs only; client gets a generic message
        response = JSONResponse({"error": "internal error"}, status_code=500)
    response.headers["X-Request-ID"] = request_id  # correlate logs <-> audit rows
    response.headers["X-Content-Type-Options"] = "nosniff"
    a = request.state.audit
    try:
        async with request.app.state.pool.connection() as conn:
            await conn.execute(
                """INSERT INTO audit_log (request_id,user_id,email,role,method,path,status,ip,detail)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (request_id, a["user_id"], a["email"], a["role"], request.method, request.url.path,
                 response.status_code, client_ip(request), Jsonb(a["detail"]) if a["detail"] else None),
            )
    except Exception:
        # In regulated environments you may prefer to fail the request instead.
        log.exception("audit write failed")
    return response
