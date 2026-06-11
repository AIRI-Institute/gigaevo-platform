"""API-key auth + CORS allowlist helpers for Master API.

Both are opt-in: when the relevant env var is unset, the service falls back
to its historical open-by-default behaviour so local dev keeps working.
"""

from __future__ import annotations

import os
from typing import List, Optional

from fastapi import Header, HTTPException, status

API_KEY_HEADER_NAME = "X-API-Key"
API_KEY_ENV = "MASTER_API_KEY"
CORS_ORIGINS_ENV = "CORS_ALLOWED_ORIGINS"


def get_configured_api_key() -> Optional[str]:
    """Return the configured API key, or None if auth is disabled."""
    value = os.getenv(API_KEY_ENV)
    if value is None:
        return None
    value = value.strip()
    return value or None


def get_cors_allowed_origins() -> List[str]:
    """Parse CORS_ALLOWED_ORIGINS env (comma-separated). Default: ['*']."""
    raw = os.getenv(CORS_ORIGINS_ENV)
    if not raw or not raw.strip():
        return ["*"]
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


async def require_api_key(x_api_key: Optional[str] = Header(default=None, alias=API_KEY_HEADER_NAME)) -> None:
    """FastAPI dependency that enforces ``X-API-Key`` when configured.

    When ``MASTER_API_KEY`` is unset, the dependency is a no-op so existing
    deployments keep working without configuration changes.
    """
    expected = get_configured_api_key()
    if expected is None:
        return
    if not x_api_key or x_api_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": API_KEY_HEADER_NAME},
        )
