"""Tests for opt-in API-key auth + CORS allowlist on Master/Runner APIs.

Verifies:
1. When ``MASTER_API_KEY`` / ``RUNNER_API_KEY`` is unset the security
   dependency is a no-op (backward compat).
2. When set, requests without ``X-API-Key`` get 401; with the right
   header they pass.
3. ``CORS_ALLOWED_ORIGINS`` is parsed correctly: unset → ``["*"]``,
   comma-separated → list; whitespace trimmed; empty entries dropped.
4. ``allow_credentials`` is forced to ``False`` when the wildcard is
   present (browsers reject `Access-Control-Allow-Origin: *` together
   with credentials).
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
MASTER_SRC = REPO_ROOT / "master_api" / "src"
RUNNER_SRC = REPO_ROOT / "runner_api" / "src"


def _import_security(service: str):
    """Import the ``security`` module of the given service in isolation.

    The two services live in sibling ``src/`` trees and both expose a
    top-level ``security`` module. We swap ``sys.path`` per test so each
    import resolves to the right one without aliasing.
    """
    target = MASTER_SRC if service == "master" else RUNNER_SRC
    sys.path.insert(0, str(target))
    try:
        if "security" in sys.modules:
            del sys.modules["security"]
        return importlib.import_module("security")
    finally:
        sys.path.remove(str(target))


# ---------------------------------------------------------------------------
# CORS env parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("service", ["master", "runner"])
def test_cors_unset_defaults_to_wildcard(service, monkeypatch):
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    sec = _import_security(service)
    assert sec.get_cors_allowed_origins() == ["*"]


@pytest.mark.parametrize("service", ["master", "runner"])
def test_cors_comma_separated_with_whitespace(service, monkeypatch):
    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGINS",
        " http://localhost:7860 , https://care.example , ,http://localhost:8000",
    )
    sec = _import_security(service)
    assert sec.get_cors_allowed_origins() == [
        "http://localhost:7860",
        "https://care.example",
        "http://localhost:8000",
    ]


@pytest.mark.parametrize("service", ["master", "runner"])
def test_cors_empty_string_falls_back_to_wildcard(service, monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "   ")
    sec = _import_security(service)
    assert sec.get_cors_allowed_origins() == ["*"]


# ---------------------------------------------------------------------------
# API key dependency
# ---------------------------------------------------------------------------


def _build_app(sec_module) -> FastAPI:
    """Stand up a tiny app wired through the service's ``require_api_key``."""
    from fastapi import Depends

    app = FastAPI()
    cors_origins = sec_module.get_cors_allowed_origins()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials="*" not in cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/protected", dependencies=[Depends(sec_module.require_api_key)])
    def protected():
        return {"ok": True}

    return app


@pytest.mark.parametrize(
    "service,env_var",
    [("master", "MASTER_API_KEY"), ("runner", "RUNNER_API_KEY")],
)
def test_no_key_configured_allows_anonymous(service, env_var, monkeypatch):
    monkeypatch.delenv(env_var, raising=False)
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    sec = _import_security(service)
    client = TestClient(_build_app(sec))

    resp = client.get("/protected")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


@pytest.mark.parametrize(
    "service,env_var",
    [("master", "MASTER_API_KEY"), ("runner", "RUNNER_API_KEY")],
)
def test_configured_key_rejects_missing_header(service, env_var, monkeypatch):
    monkeypatch.setenv(env_var, "s3cr3t-key")
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    sec = _import_security(service)
    client = TestClient(_build_app(sec))

    resp = client.get("/protected")
    assert resp.status_code == 401
    # Hint to clients on which header to send.
    assert resp.headers.get("www-authenticate") == "X-API-Key"


@pytest.mark.parametrize(
    "service,env_var",
    [("master", "MASTER_API_KEY"), ("runner", "RUNNER_API_KEY")],
)
def test_configured_key_rejects_wrong_value(service, env_var, monkeypatch):
    monkeypatch.setenv(env_var, "s3cr3t-key")
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    sec = _import_security(service)
    client = TestClient(_build_app(sec))

    resp = client.get("/protected", headers={"X-API-Key": "nope"})
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "service,env_var",
    [("master", "MASTER_API_KEY"), ("runner", "RUNNER_API_KEY")],
)
def test_configured_key_accepts_matching_header(service, env_var, monkeypatch):
    monkeypatch.setenv(env_var, "s3cr3t-key")
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    sec = _import_security(service)
    client = TestClient(_build_app(sec))

    resp = client.get("/protected", headers={"X-API-Key": "s3cr3t-key"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


@pytest.mark.parametrize(
    "service,env_var",
    [("master", "MASTER_API_KEY"), ("runner", "RUNNER_API_KEY")],
)
def test_empty_env_var_is_treated_as_unset(service, env_var, monkeypatch):
    """A literal ``API_KEY=`` (empty) must NOT lock everything out."""
    monkeypatch.setenv(env_var, "   ")
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    sec = _import_security(service)
    client = TestClient(_build_app(sec))

    resp = client.get("/protected")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# CORS response headers (smoke test through the middleware)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("service", ["master", "runner"])
def test_cors_allowlist_echoes_specific_origin(service, monkeypatch):
    monkeypatch.delenv("MASTER_API_KEY", raising=False)
    monkeypatch.delenv("RUNNER_API_KEY", raising=False)
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://localhost:7860,https://care.example")
    sec = _import_security(service)
    client = TestClient(_build_app(sec))

    resp = client.get("/protected", headers={"Origin": "http://localhost:7860"})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:7860"
    # When we restrict the allowlist we DO want credentials support.
    assert resp.headers.get("access-control-allow-credentials") == "true"


@pytest.mark.parametrize("service", ["master", "runner"])
def test_cors_allowlist_blocks_non_listed_origin(service, monkeypatch):
    monkeypatch.delenv("MASTER_API_KEY", raising=False)
    monkeypatch.delenv("RUNNER_API_KEY", raising=False)
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://localhost:7860")
    sec = _import_security(service)
    client = TestClient(_build_app(sec))

    resp = client.get("/protected", headers={"Origin": "https://evil.example"})
    # The request itself still completes (CORS is enforced by browsers, not
    # the server), but the middleware must NOT echo the rejected origin.
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") is None
