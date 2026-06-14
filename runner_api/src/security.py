"""API-key auth + CORS allowlist helpers for Runner API.

Mirrors ``master_api.src.security`` but reads ``RUNNER_API_KEY`` so the two
services can carry independent keys when desired.
"""

from __future__ import annotations

import os
from typing import List, Optional

from fastapi import Header, HTTPException, status

API_KEY_HEADER_NAME = "X-API-Key"
API_KEY_ENV = "RUNNER_API_KEY"
CORS_ORIGINS_ENV = "CORS_ALLOWED_ORIGINS"


def get_configured_api_key() -> Optional[str]:
    value = os.getenv(API_KEY_ENV)
    if value is None:
        return None
    value = value.strip()
    return value or None


def get_cors_allowed_origins() -> List[str]:
    raw = os.getenv(CORS_ORIGINS_ENV)
    if not raw or not raw.strip():
        return ["*"]
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


async def require_api_key(x_api_key: Optional[str] = Header(default=None, alias=API_KEY_HEADER_NAME)) -> None:
    expected = get_configured_api_key()
    if expected is None:
        return
    if not x_api_key or x_api_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": API_KEY_HEADER_NAME},
        )
