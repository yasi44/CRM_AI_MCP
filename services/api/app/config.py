"""Settings come from environment variables (12-factor). Secrets are never hard-coded."""
import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    database_url: str
    jwt_secret: str          # HS256 key shared with the MCP + agent services
    token_ttl_seconds: int = 900   # short-lived tokens limit damage if one leaks
    slack_webhook: str = ""  # optional third-party integration
    data_dir: str = "/data"  # where seed CSVs live


@lru_cache
def get_settings() -> Settings:
    """Loaded lazily (not at import) so unit tests can import modules without env vars."""
    s = Settings(
        database_url=os.environ.get("DATABASE_URL", ""),
        jwt_secret=os.environ.get("JWT_SECRET", ""),
        slack_webhook=os.environ.get("SLACK_WEBHOOK_URL", ""),
        data_dir=os.environ.get("DATA_DIR", "/data"),
    )
    # Fail fast on insecure configuration instead of running unsafely.
    if not s.database_url:
        raise RuntimeError("DATABASE_URL is required")
    if len(s.jwt_secret) < 32:
        raise RuntimeError("JWT_SECRET must be at least 32 characters")
    return s
