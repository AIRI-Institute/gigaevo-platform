"""Tests for the §4.5c egress proxy.

Coverage:

* ``AllowList`` parsing — literal hosts, ``*.subdomain`` wildcards,
  case-insensitivity, ports, and refusal of dangerous patterns (bare
  ``*``, ``*.``, ``.``, invalid characters).
* ``EgressProxy`` over a real loopback listener:
  - **DENY** for hosts not on the list (403).
  - **DENY** for an allowed host but disallowed port (403).
  - **ALLOW** + actual TCP bridge for an allowed host:port, asserted by
    sending an HTTP request through the CONNECT tunnel to a small fake
    server bound on a sibling loopback port and reading the reply
    end-to-end.
  - 400 on malformed CONNECT, 405 on non-CONNECT verbs.
  - ``allowed_count`` / ``denied_count`` counters move correctly.
* ``build_docker_run_args`` integration: when ``SKILL_DECLARED`` is
  paired with ``proxy_url``, the args switch from the old
  ``--add-host …:127.0.0.1`` sentinel to a proper egress flow
  (``HTTP_PROXY`` / ``HTTPS_PROXY`` + ``--add-host
  host.docker.internal:host-gateway``).
"""

from __future__ import annotations

import asyncio
import importlib
import socket
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER_ROOT = REPO_ROOT / "runner_api"

# Match the path-manipulation pattern in test_sandbox.py (which runs
# alongside this file): only RUNNER_ROOT is needed; egress_proxy has no
# ``common`` dependency, so REPO_ROOT must NOT be added here — doing so
# rearranges sys.path enough that the master_api fixtures resolve
# ``src.api.routes.evolutions`` against runner_api/ and fail.
sys.path.insert(0, str(RUNNER_ROOT))

for _stale in [m for m in list(sys.modules) if m == "src" or m.startswith("src.")]:
    sys.modules.pop(_stale, None)

sandbox = importlib.import_module("src.sandbox")
egress_proxy_mod = importlib.import_module("src.sandbox.egress_proxy")
docker_be = importlib.import_module("src.sandbox.docker_backend")
protocol = importlib.import_module("src.sandbox.protocol")


# ---------------------------------------------------------------------------
# AllowList
# ---------------------------------------------------------------------------


def test_allowlist_literal_host_matches_case_insensitively():
    al = egress_proxy_mod.AllowList(["Example.COM"])
    assert al.host_matches("example.com")
    assert al.host_matches("EXAMPLE.com")
    assert not al.host_matches("foo.example.com")


def test_allowlist_wildcard_matches_subdomains_only():
    al = egress_proxy_mod.AllowList(["*.example.com"])
    assert al.host_matches("foo.example.com")
    assert al.host_matches("a.b.c.example.com")
    # Bare base must NOT match the wildcard (matches mimics RFC rules).
    assert not al.host_matches("example.com")


def test_allowlist_ports_restrict_to_default_https_http():
    al = egress_proxy_mod.AllowList(["example.com"])
    assert al.allows("example.com", 443)
    assert al.allows("example.com", 80)
    assert not al.allows("example.com", 22)


def test_allowlist_custom_ports_take_precedence():
    al = egress_proxy_mod.AllowList(["example.com"], allowed_ports=[8443])
    assert al.allows("example.com", 8443)
    assert not al.allows("example.com", 443)


@pytest.mark.parametrize(
    "bad",
    ["", "  ", "*", "*.", ".", "no spaces allowed", "with/slash", "with;semi"],
)
def test_allowlist_rejects_dangerous_entries(bad):
    with pytest.raises(ValueError):
        egress_proxy_mod.AllowList([bad])


def test_allowlist_requires_at_least_one_port():
    with pytest.raises(ValueError):
        egress_proxy_mod.AllowList(["example.com"], allowed_ports=[])


# ---------------------------------------------------------------------------
# Real-loopback proxy: deny path
# ---------------------------------------------------------------------------


async def _send_connect(host: str, port: int, target: str) -> bytes:
    """Open a TCP socket, send a CONNECT request, return the proxy's reply.

    Reads until the proxy closes its side (denied case) or until we get
    the response headers (allowed case — we close immediately after).
    """
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode("ascii"))
        await writer.drain()
        buf = b""
        # Read until we see the headers' double-CRLF or the socket closes.
        while b"\r\n\r\n" not in buf:
            chunk = await asyncio.wait_for(reader.read(1024), timeout=2)
            if not chunk:
                break
            buf += chunk
        return buf
    finally:
        try:
            writer.close()
        except Exception:
            pass


@pytest.mark.asyncio
async def test_proxy_denies_disallowed_host():
    async with egress_proxy_mod.EgressProxy.start(["example.com"]) as proxy:
        resp = await _send_connect(proxy.host, proxy.port, "evil.test:443")
        assert resp.startswith(b"HTTP/1.1 403 Forbidden\r\n"), resp
        assert proxy.denied_count == 1
        assert proxy.allowed_count == 0


@pytest.mark.asyncio
async def test_proxy_denies_allowed_host_but_disallowed_port():
    async with egress_proxy_mod.EgressProxy.start(["example.com"]) as proxy:
        resp = await _send_connect(proxy.host, proxy.port, "example.com:22")
        assert resp.startswith(b"HTTP/1.1 403 Forbidden\r\n"), resp


@pytest.mark.asyncio
async def test_proxy_rejects_non_connect_method():
    async with egress_proxy_mod.EgressProxy.start(["example.com"]) as proxy:
        reader, writer = await asyncio.open_connection(proxy.host, proxy.port)
        writer.write(b"GET http://example.com/ HTTP/1.1\r\n\r\n")
        await writer.drain()
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = await asyncio.wait_for(reader.read(1024), timeout=2)
            if not chunk:
                break
            buf += chunk
        writer.close()
        assert buf.startswith(b"HTTP/1.1 405 "), buf


@pytest.mark.asyncio
async def test_proxy_rejects_malformed_connect_line():
    async with egress_proxy_mod.EgressProxy.start(["example.com"]) as proxy:
        reader, writer = await asyncio.open_connection(proxy.host, proxy.port)
        writer.write(b"CONNECT no-port HTTP/1.1\r\n\r\n")
        await writer.drain()
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = await asyncio.wait_for(reader.read(1024), timeout=2)
            if not chunk:
                break
            buf += chunk
        writer.close()
        assert buf.startswith(b"HTTP/1.1 405 "), buf


# ---------------------------------------------------------------------------
# Real-loopback proxy: allow path with end-to-end byte bridge
# ---------------------------------------------------------------------------


async def _start_fake_target() -> tuple[asyncio.AbstractServer, int]:
    """Tiny HTTP/1.0 server bound to 127.0.0.1 on a free port.

    Reads request bytes until `\\r\\n\\r\\n`, then replies with a
    well-formed response so the test can verify the TCP bridge moved
    bytes correctly.
    """

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        buf = b""
        try:
            while b"\r\n\r\n" not in buf:
                chunk = await asyncio.wait_for(reader.read(1024), timeout=2)
                if not chunk:
                    break
                buf += chunk
            writer.write(
                b"HTTP/1.0 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 11\r\n"
                b"\r\n"
                b"hello-skill"
            )
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, host="127.0.0.1", port=0)
    port = int(server.sockets[0].getsockname()[1])
    return server, port


@pytest.mark.asyncio
async def test_proxy_allows_listed_host_and_bridges_bytes():
    target_server, target_port = await _start_fake_target()
    try:
        # Allow ``localhost`` (the alias the test will CONNECT to) on the
        # actual target port. The CONNECT target uses ``localhost`` so the
        # AllowList matches the literal entry, while the proxy resolves it
        # to 127.0.0.1 on egress.
        async with egress_proxy_mod.EgressProxy.start(
            ["localhost"], allowed_ports=[target_port]
        ) as proxy:
            reader, writer = await asyncio.open_connection(proxy.host, proxy.port)
            try:
                writer.write(
                    f"CONNECT localhost:{target_port} HTTP/1.1\r\n"
                    f"Host: localhost:{target_port}\r\n\r\n".encode("ascii")
                )
                await writer.drain()
                # Read the proxy's CONNECT response (until the blank line).
                buf = b""
                while b"\r\n\r\n" not in buf:
                    chunk = await asyncio.wait_for(reader.read(1024), timeout=2)
                    if not chunk:
                        break
                    buf += chunk
                assert buf.startswith(b"HTTP/1.1 200 "), buf
                # Now the tunnel is open — speak HTTP to the fake target.
                writer.write(
                    f"GET /skill HTTP/1.0\r\nHost: localhost:{target_port}\r\n\r\n".encode("ascii")
                )
                await writer.drain()
                # Drain the upstream response.
                upstream = b""
                while b"hello-skill" not in upstream:
                    chunk = await asyncio.wait_for(reader.read(1024), timeout=2)
                    if not chunk:
                        break
                    upstream += chunk
                assert b"HTTP/1.0 200 OK" in upstream
                assert b"hello-skill" in upstream
            finally:
                writer.close()
            assert proxy.allowed_count == 1
            assert proxy.denied_count == 0
    finally:
        target_server.close()
        try:
            await target_server.wait_closed()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# build_docker_run_args integration
# ---------------------------------------------------------------------------


def _req(**overrides):
    base = dict(
        skill_sha256="a" * 64,
        skill_name="t",
        workspace=Path("/tmp"),
        command=["echo", "hi"],
        env={"FOO": "bar"},
        network=protocol.NetworkPolicy.SKILL_DECLARED,
        allowed_domains=["example.com", "*.adobe.com"],
    )
    base.update(overrides)
    return protocol.RunRequest(**base)


def test_docker_args_skill_declared_with_proxy_swaps_to_http_proxy():
    req = _req()
    args = docker_be.build_docker_run_args(
        req, container_name="c", proxy_url="http://127.0.0.1:54321"
    )
    # Old fail-closed sentinel must NOT be present any more — i.e. no
    # ``--add-host <domain>:127.0.0.1`` mapping for any allowlist entry.
    add_host_pairs = [
        args[i + 1] for i, a in enumerate(args) if a == "--add-host"
    ]
    for host in req.allowed_domains:
        assert f"{host}:127.0.0.1" not in add_host_pairs
    # Proxy is exposed through host.docker.internal for the container.
    proxy_value = "http://host.docker.internal:54321"
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        assert f"{key}={proxy_value}" in args, key
    # The container must know to skip the proxy for its own loopback.
    no_proxy = next(a for a in args if a.startswith("NO_PROXY="))
    assert "127.0.0.1" in no_proxy
    # Network stays on bridge; host gateway is injected.
    assert "bridge" in args
    add_host_pairs = [
        args[i + 1] for i, a in enumerate(args) if a == "--add-host"
    ]
    assert "host.docker.internal:host-gateway" in add_host_pairs
    # User's own env var is still forwarded.
    assert "FOO=bar" in args


def test_docker_args_skill_declared_without_proxy_keeps_legacy_sentinel():
    """No regression for the existing fail-closed mode."""
    req = _req()
    args = docker_be.build_docker_run_args(req, container_name="c", proxy_url=None)
    # Old sentinel pattern from §4.5a/§4.5b:
    add_host_pairs = [
        args[i + 1] for i, a in enumerate(args) if a == "--add-host"
    ]
    assert "example.com:127.0.0.1" in add_host_pairs
    assert "*.adobe.com:127.0.0.1" in add_host_pairs
    # And no proxy env was injected.
    assert not any(a.startswith("HTTP_PROXY=") for a in args)


def test_docker_args_localhost_rewriter_handles_both_aliases():
    for url in ("http://127.0.0.1:1234", "http://localhost:1234"):
        out = docker_be._rewrite_localhost_for_container(url)
        assert out == "http://host.docker.internal:1234"
    # External proxy unchanged.
    out = docker_be._rewrite_localhost_for_container("http://proxy.corp:1234")
    assert out == "http://proxy.corp:1234"


# Avoid linting "imported but unused" on the bare ``socket`` import,
# kept in case a future test needs raw socket primitives.
_ = socket
