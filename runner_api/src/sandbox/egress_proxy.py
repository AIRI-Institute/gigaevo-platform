"""HTTP-CONNECT egress proxy for ``NetworkPolicy.SKILL_DECLARED`` (CARE §4.5c).

What this is for
================

When a SKILL.md declares ``WebFetch(domain:example.com)`` in its
``allowed-tools`` frontmatter, the sandbox needs to grant the container
network access to *exactly* that host — and nothing else. ``§4.5a`` /
``§4.5b`` shipped a placeholder that mapped every allowed host to
``127.0.0.1`` via ``--add-host``; that's a fail-closed sentinel, not real
egress. This module replaces it with a tiny per-run HTTP-CONNECT proxy
the sandboxed process talks to via the standard ``HTTP_PROXY`` /
``HTTPS_PROXY`` env vars.

Design
======

* :class:`AllowList` parses the SKILL.md token list into hosts +
  optional wildcards (``*.example.com``). Matching is case-insensitive
  and rejects bare ``*`` so a typo can't open the door to everything.
* :class:`EgressProxy` is an ``asyncio`` TCP server speaking just
  enough of HTTP/1.1 to handle CONNECT. Allowed targets get a
  bidirectional byte splice to the real host; denied ones get a
  ``403 Forbidden`` and a closed socket. Ports are restricted by
  default to ``{80, 443}`` so a CONNECT to ``example.com:22`` against
  an ``example.com`` allowlist entry still fails closed.
* Lifecycle: ``async with EgressProxy.start(...) as proxy:`` opens a
  loopback listener, hands you ``proxy.url`` ready to inject as
  ``HTTP_PROXY``, and on exit closes the listener and severs any
  in-flight connections.

Non-goals (yet)
===============

* No TLS termination — CONNECT bridges raw bytes.
* No upstream HTTP parsing for non-CONNECT methods (the container
  doesn't need it; ``requests`` / ``httpx`` issue CONNECT for HTTPS
  and direct-relay GET for plain HTTP, but skill workloads almost
  always hit HTTPS).
* No connection pooling against upstreams; each CONNECT opens a fresh
  TCP socket. SKILL-sized workloads won't notice.
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from typing import AsyncIterator, FrozenSet, Iterable, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_ALLOWED_PORTS: FrozenSet[int] = frozenset({80, 443})

_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9.\-*]+$")


class AllowList:
    """Host (+ port) allowlist with subdomain wildcards.

    Each entry is a literal hostname or a wildcard like ``*.example.com``
    that matches any single-or-multi-component subdomain of
    ``example.com`` (matching mimics RFC-style wildcard rules: the bare
    base is NOT matched by ``*.base``). All matching is
    case-insensitive.
    """

    def __init__(
        self,
        hosts: Iterable[str],
        *,
        allowed_ports: Iterable[int] = DEFAULT_ALLOWED_PORTS,
    ) -> None:
        literal: set[str] = set()
        suffixes: set[str] = set()
        for raw in hosts:
            host = (raw or "").strip().lower()
            if not host or not _HOSTNAME_RE.match(host):
                raise ValueError(f"invalid allowlist entry: {raw!r}")
            if host == "*" or host == "*." or host == ".":
                raise ValueError(f"refusing to broaden allowlist with bare wildcard: {raw!r}")
            if host.startswith("*."):
                suffix = host[2:]
                if not suffix or suffix.startswith("."):
                    raise ValueError(f"invalid wildcard allowlist entry: {raw!r}")
                suffixes.add(suffix)
            else:
                literal.add(host)
        self._literal: FrozenSet[str] = frozenset(literal)
        self._suffixes: FrozenSet[str] = frozenset(suffixes)
        self._ports: FrozenSet[int] = frozenset(int(p) for p in allowed_ports)
        if not self._ports:
            raise ValueError("AllowList requires at least one allowed port")

    @property
    def literals(self) -> FrozenSet[str]:
        return self._literal

    @property
    def wildcard_suffixes(self) -> FrozenSet[str]:
        return self._suffixes

    @property
    def ports(self) -> FrozenSet[int]:
        return self._ports

    def host_matches(self, host: str) -> bool:
        host = (host or "").strip().lower()
        if not host:
            return False
        if host in self._literal:
            return True
        for suffix in self._suffixes:
            # Require a leading subdomain — bare ``base`` doesn't match
            # ``*.base``.
            if host.endswith("." + suffix) and len(host) > len(suffix) + 1:
                return True
        return False

    def allows(self, host: str, port: int) -> bool:
        return port in self._ports and self.host_matches(host)


class EgressProxyError(Exception):
    pass


def _parse_connect_line(line: bytes) -> Tuple[str, int]:
    """Pull (host, port) out of ``CONNECT host:port HTTP/1.1``."""
    try:
        text = line.decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise EgressProxyError(f"non-ASCII request line: {exc}") from exc
    parts = text.split()
    if len(parts) < 2 or parts[0].upper() != "CONNECT":
        raise EgressProxyError(f"not a CONNECT request: {text.strip()!r}")
    target = parts[1]
    if ":" not in target:
        raise EgressProxyError(f"CONNECT target missing port: {target!r}")
    host, _, port_str = target.rpartition(":")
    if not host:
        raise EgressProxyError(f"empty host in CONNECT: {target!r}")
    try:
        port = int(port_str)
    except ValueError as exc:
        raise EgressProxyError(f"invalid port in CONNECT: {port_str!r}") from exc
    if not 1 <= port <= 65535:
        raise EgressProxyError(f"port out of range: {port}")
    return host.lower(), port


async def _read_until_double_crlf(reader: asyncio.StreamReader, max_bytes: int = 8192) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = await reader.read(1024)
        if not chunk:
            break
        data += chunk
        if len(data) > max_bytes:
            raise EgressProxyError("CONNECT preamble exceeds 8 KiB")
    return data


async def _splice(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await src.read(65536)
            if not chunk:
                break
            dst.write(chunk)
            await dst.drain()
    finally:
        try:
            dst.close()
        except Exception:  # pragma: no cover — defensive
            pass


class EgressProxy:
    """Per-run HTTP-CONNECT proxy bound to ``127.0.0.1``."""

    def __init__(self, allowlist: AllowList) -> None:
        self._allowlist = allowlist
        self._server: Optional[asyncio.AbstractServer] = None
        self._host = "127.0.0.1"
        self._port: Optional[int] = None
        # Stats useful for tests and ``/debug`` introspection.
        self.allowed_count = 0
        self.denied_count = 0

    @classmethod
    @asynccontextmanager
    async def start(
        cls,
        allowed_domains: Iterable[str],
        *,
        allowed_ports: Iterable[int] = DEFAULT_ALLOWED_PORTS,
    ) -> AsyncIterator["EgressProxy"]:
        """Open the listener and yield a running :class:`EgressProxy`."""
        allowlist = AllowList(allowed_domains, allowed_ports=allowed_ports)
        proxy = cls(allowlist)
        await proxy._open()
        try:
            yield proxy
        finally:
            await proxy._close()

    @property
    def allowlist(self) -> AllowList:
        return self._allowlist

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        if self._port is None:
            raise EgressProxyError("proxy is not running")
        return self._port

    @property
    def url(self) -> str:
        """Value to drop into ``HTTP_PROXY`` / ``HTTPS_PROXY``."""
        return f"http://{self.host}:{self.port}"

    async def _open(self) -> None:
        self._server = await asyncio.start_server(self._handle, host=self._host, port=0)
        socket_obj = self._server.sockets[0]
        self._port = int(socket_obj.getsockname()[1])
        logger.info(f"egress proxy listening on {self.url}")

    async def _close(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # pragma: no cover
                pass
        self._server = None

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        upstream_writer: Optional[asyncio.StreamWriter] = None
        try:
            try:
                preamble = await asyncio.wait_for(_read_until_double_crlf(reader), timeout=10)
            except asyncio.TimeoutError:
                await self._respond(writer, 408, "Request Timeout")
                self.denied_count += 1
                return
            except EgressProxyError as exc:
                logger.info(f"egress proxy: bad preamble: {exc}")
                await self._respond(writer, 400, "Bad Request")
                self.denied_count += 1
                return

            request_line, _, _ = preamble.partition(b"\r\n")
            try:
                host, port = _parse_connect_line(request_line)
            except EgressProxyError as exc:
                logger.info(f"egress proxy: rejecting non-CONNECT: {exc}")
                await self._respond(writer, 405, "Method Not Allowed")
                self.denied_count += 1
                return

            if not self._allowlist.allows(host, port):
                logger.info(f"egress proxy: DENY {host}:{port}")
                await self._respond(writer, 403, "Forbidden")
                self.denied_count += 1
                return

            try:
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=10
                )
            except (OSError, asyncio.TimeoutError) as exc:
                logger.info(f"egress proxy: upstream {host}:{port} unreachable: {exc}")
                await self._respond(writer, 502, "Bad Gateway")
                self.denied_count += 1
                return

            await self._respond(writer, 200, "Connection Established")
            self.allowed_count += 1
            logger.info(f"egress proxy: ALLOW {host}:{port}")

            await asyncio.gather(
                _splice(reader, upstream_writer),
                _splice(upstream_reader, writer),
            )
        finally:
            try:
                writer.close()
            except Exception:  # pragma: no cover
                pass
            if upstream_writer is not None:
                try:
                    upstream_writer.close()
                except Exception:  # pragma: no cover
                    pass

    async def _respond(self, writer: asyncio.StreamWriter, status: int, reason: str) -> None:
        try:
            writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Length: 0\r\n\r\n".encode("ascii"))
            await writer.drain()
        except Exception:  # pragma: no cover — client may already be gone
            pass
