"""Shared HTTP session for web_ui → Master API calls.

When ``GIGAEVO_API_KEY`` is set, every outgoing request from the session
carries an ``X-API-Key`` header. When the env var is unset, the session
behaves like a stock ``requests.Session`` (matches pre-auth defaults).
"""

from __future__ import annotations

import os

import requests


def make_session() -> requests.Session:
    """Return a ``requests.Session`` that forwards ``GIGAEVO_API_KEY``."""
    session = requests.Session()
    api_key = (os.getenv("GIGAEVO_API_KEY") or "").strip()
    if api_key:
        session.headers["X-API-Key"] = api_key
    return session
