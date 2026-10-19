"""Authentication (JWT) and authorization (RBAC).

Roles map to PERMISSIONS, and routes check permissions, not role names.
That keeps routes stable when you add roles or change what a role may do.
"""
import time
from dataclasses import dataclass

import bcrypt
import jwt
from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi import Depends

from .config import get_settings

# ---------------------------------------------------------------- RBAC ----
VIEWER = frozenset({"accounts:read", "opportunities:read", "analytics:read"})  # NO money fields (privacy)
REP = VIEWER | {"opportunities:write", "revenue:read", "deal_value:read"}
MANAGER = REP | {"audit:read"}
ADMIN = MANAGER | {"users:manage"}
ROLE_PERMS = {"viewer": VIEWER, "rep": REP, "manager": MANAGER, "admin": ADMIN}


def has_perm(role: str, perm: str) -> bool:
    return perm in ROLE_PERMS.get(role, frozenset())  # unknown role => deny by default


# ------------------------------------------------------------ passwords ----
def _pw(password: str) -> bytes:
    return password.encode()[:72]  # bcrypt only uses the first 72 bytes


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_pw(password), bcrypt.gensalt()).decode()


def check_password(password: str, hashed: bytes) -> bool:
    return bcrypt.checkpw(_pw(password), hashed)


# Used when the email does not exist, so response time doesn't reveal which emails are registered.
DUMMY_HASH = bcrypt.hashpw(b"dummy-password-for-timing", bcrypt.gensalt())


# --------------------------------------------------------------- tokens ----
def issue_token(user_id: str, email: str, role: str) -> str:
    s = get_settings()
    now = int(time.time())
    claims = {
        "sub": user_id, "email": email, "role": role,
        "iss": "crm-api", "aud": "crm",       # validated on every request, here and in the Node services
        "iat": now, "exp": now + s.token_ttl_seconds,
    }
    return jwt.encode(claims, s.jwt_secret, algorithm="HS256")


@dataclass
class Principal:
    id: str
    email: str
    role: str


bearer = HTTPBearer(auto_error=False)


def require(perm: str | None = None):
    """FastAPI dependency factory = authentication + authorization for one route.
    perm=None means 'any authenticated user'."""

    async def dependency(request: Request, creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
        if creds is None:
            raise HTTPException(401, "missing bearer token")
        try:
            claims = jwt.decode(
                creds.credentials, get_settings().jwt_secret,
                algorithms=["HS256"],                       # pinned: blocks the "alg=none" attack
                issuer="crm-api", audience="crm",
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError:
            raise HTTPException(401, "invalid or expired token")
        p = Principal(claims["sub"], claims.get("email", ""), claims.get("role", ""))
        # Tell the audit middleware who this was (it logs AFTER the response, even for 403s).
        request.state.audit.update(user_id=p.id, email=p.email, role=p.role)
        if perm and not has_perm(p.role, perm):
            raise HTTPException(403, f"forbidden: requires {perm}")
        return p

    return dependency
