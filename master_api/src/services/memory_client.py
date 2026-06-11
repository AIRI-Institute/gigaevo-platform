"""Thin async HTTP client for ``gigaevo-memory`` (CARE §4.6).

The platform stays loosely coupled to Memory: this client knows just
enough to push two artefact types — recorded individuals (`chain`
entities with ``evolution_meta`` populated) and accepted chains (same
entity, promoted to the ``stable`` channel).

Configuration is read from ``Config.memory_api_url`` (env
``MEMORY_API_URL``). When unset, :class:`MemoryClient` becomes a no-op:
every method returns ``None`` / ``False`` without touching the network.
This is deliberate — local development and the existing test suite must
keep working without a Memory deployment.

All calls are **best-effort**: HTTP errors are logged but never raised.
The platform's evolution flow keeps moving even when Memory is down,
and CARE can backfill missing records later via the §4.2 list/get APIs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger

DEFAULT_TIMEOUT_SECONDS = 5.0


class MemoryPromotionError(RuntimeError):
    """Raised when the strict accept-winner Memory transaction fails."""


@dataclass(frozen=True)
class MemoryPromotionResult:
    """Version metadata returned after advancing a chain's latest pointer."""

    chain_id: str
    previous_version: Optional[int]
    new_version: Optional[int]
    new_version_id: Optional[str]


@dataclass(frozen=True)
class MemoryLatestChain:
    """Latest channel metadata needed for an optimistic Memory update."""

    chain_id: str
    version_number: Optional[int]
    version_id: Optional[str]
    etag: Optional[str]
    meta: Optional[Dict[str, Any]]


@dataclass(frozen=True)
class MemoryVersionInfo:
    """Version-list metadata used to make accept retries idempotent."""

    version_number: Optional[int]
    version_id: Optional[str]
    evolution_meta: Optional[Dict[str, Any]]


class MemoryClient:
    """Async wrapper around the gigaevo-memory HTTP surface.

    The two methods CARE relies on:

    * :meth:`save_chain` — POST /v1/chains with ``content`` + ``evolution_meta``.
    * :meth:`set_channel` — PATCH /v1/chains/{id}/channel to promote
      to ``stable`` after a §4.4 accept.

    Both return the entity id on success and ``None`` on any failure
    (network error, non-2xx, missing config), so callers can keep their
    own state-machine moving without sprinkling try/except around every
    use site.
    """

    def __init__(
        self,
        base_url: Optional[str],
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/") if base_url else None
        self._timeout = timeout_seconds
        # ``client`` is injectable for tests (MockTransport). When None,
        # we lazily build one per request so a long-lived caller with
        # an unset MEMORY_API_URL never opens a connection pool.
        self._client = client
        self._owns_client = client is None

    @property
    def is_configured(self) -> bool:
        """True when ``MEMORY_API_URL`` is set and not blank."""
        return bool(self._base_url)

    async def save_chain(
        self,
        *,
        name: str,
        content: Dict[str, Any],
        evolution_meta: Dict[str, Any],
        tags: Optional[List[str]] = None,
        description: Optional[str] = None,
        channel: str = "latest",
    ) -> Optional[str]:
        """POST a chain entity. Returns the entity id or None on failure."""
        if not self.is_configured:
            return None
        payload = {
            "meta": {
                "name": name,
                "tags": list(tags or []),
                "when_to_use": description,
            },
            "channel": channel,
            "content": content,
            "evolution_meta": evolution_meta,
        }
        try:
            response = await self._request("POST", "/v1/chains", json=payload)
        except httpx.HTTPError as exc:
            logger.warning(f"memory.save_chain failed: {exc}")
            return None
        if response.status_code >= 400:
            logger.warning(
                f"memory.save_chain returned HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
            return None
        try:
            body = response.json()
        except ValueError:
            logger.warning("memory.save_chain returned non-JSON body")
            return None
        entity_id = body.get("entity_id") or body.get("id")
        if not entity_id:
            logger.warning(f"memory.save_chain response missing entity_id: {body!r}")
            return None
        return str(entity_id)

    async def set_channel(self, entity_id: str, *, channel: str) -> bool:
        """Promote / demote a chain to ``channel``. Returns success."""
        if not self.is_configured:
            return False
        try:
            response = await self._request(
                "PATCH",
                f"/v1/chains/{entity_id}/channel",
                json={"channel": channel},
            )
        except httpx.HTTPError as exc:
            logger.warning(f"memory.set_channel failed: {exc}")
            return False
        if response.status_code >= 400:
            logger.warning(
                f"memory.set_channel({channel}) on {entity_id} returned "
                f"HTTP {response.status_code}: {response.text[:200]}"
            )
            return False
        return True

    async def promote_evolved_chain(
        self,
        *,
        chain_id: str,
        content: Dict[str, Any],
        evolution_meta: Dict[str, Any],
        name: str,
        tags: Optional[List[str]] = None,
        description: Optional[str] = None,
    ) -> MemoryPromotionResult:
        """Strictly write a new version under ``chain_id`` and advance ``latest``.

        Unlike :meth:`save_chain`, this method is used by the accept-winner
        path where partial state is worse than a visible failure. Any Memory
        error raises :class:`MemoryPromotionError` so the platform can leave
        its own evolution state untouched.
        """
        if not self.is_configured:
            raise MemoryPromotionError("Memory API URL is not configured")
        if not chain_id:
            raise MemoryPromotionError("chain_id must be non-empty")

        latest = await self._get_latest_chain(chain_id)
        acceptance_id = _acceptance_id(evolution_meta)
        accepted_version = await self._latest_acceptance_version(chain_id, acceptance_id)
        if accepted_version is not None:
            return MemoryPromotionResult(
                chain_id=chain_id,
                previous_version=_metrics_int(
                    accepted_version.evolution_meta, "evolved_from_version"
                ),
                new_version=accepted_version.version_number,
                new_version_id=accepted_version.version_id,
            )

        version_meta = _with_evolved_from(evolution_meta, latest.version_number)
        payload = {
            "channel": "latest",
            "content": content,
            "evolution_meta": version_meta,
            "parent_version_id": latest.version_id,
            "change_summary": "Accepted evolved chain from gigaevo-platform",
        }
        if latest.meta:
            payload["meta"] = latest.meta
        headers = {"If-Match": latest.etag} if latest.etag else None
        try:
            response = await self._request(
                "PUT",
                f"/v1/chains/{chain_id}",
                json=payload,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise MemoryPromotionError(f"Memory version write failed: {exc}") from exc
        if response.status_code == 412:
            accepted_version = await self._latest_acceptance_version(chain_id, acceptance_id)
            if accepted_version is not None:
                return MemoryPromotionResult(
                    chain_id=chain_id,
                    previous_version=_metrics_int(
                        accepted_version.evolution_meta, "evolved_from_version"
                    ),
                    new_version=accepted_version.version_number,
                    new_version_id=accepted_version.version_id,
                )
        if response.status_code >= 400:
            raise MemoryPromotionError(
                f"Memory version write returned HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise MemoryPromotionError("Memory version write returned non-JSON body") from exc

        returned_id = str(body.get("entity_id") or body.get("id") or chain_id)
        if returned_id != chain_id:
            raise MemoryPromotionError(
                f"Memory returned entity_id={returned_id!r}; expected {chain_id!r}"
            )
        return MemoryPromotionResult(
            chain_id=chain_id,
            previous_version=latest.version_number,
            new_version=_coerce_int(body.get("version_number") or body.get("new_version")),
            new_version_id=_coerce_str(body.get("version_id")),
        )

    async def _get_latest_chain(self, chain_id: str) -> MemoryLatestChain:
        try:
            response = await self._request(
                "GET",
                f"/v1/chains/{chain_id}",
                params={"channel": "latest"},
            )
        except httpx.HTTPError as exc:
            raise MemoryPromotionError(f"Memory latest lookup failed: {exc}") from exc
        if response.status_code == 404:
            raise MemoryPromotionError(f"Memory chain {chain_id!r} not found")
        if response.status_code >= 400:
            raise MemoryPromotionError(
                f"Memory latest lookup returned HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise MemoryPromotionError("Memory latest lookup returned non-JSON body") from exc
        returned_id = str(body.get("entity_id") or body.get("id") or chain_id)
        if returned_id != chain_id:
            raise MemoryPromotionError(
                f"Memory returned entity_id={returned_id!r}; expected {chain_id!r}"
            )
        candidates = (
            body.get("version_number"),
            body.get("version", {}).get("version_number") if isinstance(body.get("version"), dict) else None,
            body.get("metadata", {}).get("version_number") if isinstance(body.get("metadata"), dict) else None,
        )
        version_number = None
        for value in candidates:
            coerced = _coerce_int(value)
            if coerced is not None:
                version_number = coerced
                break
        return MemoryLatestChain(
            chain_id=chain_id,
            version_number=version_number,
            version_id=_coerce_str(body.get("version_id")),
            etag=_coerce_str(body.get("etag")),
            meta=body.get("meta") if isinstance(body.get("meta"), dict) else None,
        )

    async def _latest_acceptance_version(
        self,
        chain_id: str,
        acceptance_id: Optional[str],
    ) -> Optional[MemoryVersionInfo]:
        if not acceptance_id:
            return None
        try:
            response = await self._request(
                "GET",
                f"/v1/chains/{chain_id}/versions",
                params={"limit": 1},
            )
        except httpx.HTTPError as exc:
            raise MemoryPromotionError(f"Memory version lookup failed: {exc}") from exc
        if response.status_code >= 400:
            raise MemoryPromotionError(
                f"Memory version lookup returned HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise MemoryPromotionError("Memory version lookup returned non-JSON body") from exc
        versions = body.get("items", []) if isinstance(body, dict) else body
        if not versions:
            return None
        latest = versions[0]
        if not isinstance(latest, dict):
            return None
        evolution_meta = latest.get("evolution_meta")
        if not _matches_acceptance(evolution_meta, acceptance_id):
            return None
        return MemoryVersionInfo(
            version_number=_coerce_int(latest.get("version_number")),
            version_id=_coerce_str(latest.get("version_id")),
            evolution_meta=evolution_meta if isinstance(evolution_meta, dict) else None,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        url = f"{self._base_url}{path}"
        if self._client is not None:
            return await self._client.request(
                method,
                url,
                json=json,
                params=params,
                headers=headers,
                timeout=self._timeout,
            )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.request(method, url, json=json, params=params, headers=headers)

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()


def _coerce_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text or None


def _acceptance_id(evolution_meta: Dict[str, Any]) -> Optional[str]:
    metrics = evolution_meta.get("metrics")
    if not isinstance(metrics, dict):
        return None
    return _coerce_str(metrics.get("acceptance_id"))


def _with_evolved_from(
    evolution_meta: Dict[str, Any],
    previous_version: Optional[int],
) -> Dict[str, Any]:
    version_meta = dict(evolution_meta)
    metrics = dict(version_meta.get("metrics") or {})
    metrics.setdefault("evolved_from_version", previous_version)
    version_meta["metrics"] = metrics
    return version_meta


def _matches_acceptance(evolution_meta: Any, acceptance_id: str) -> bool:
    if not isinstance(evolution_meta, dict):
        return False
    return _acceptance_id(evolution_meta) == acceptance_id


def _metrics_int(evolution_meta: Optional[Dict[str, Any]], key: str) -> Optional[int]:
    if not isinstance(evolution_meta, dict):
        return None
    metrics = evolution_meta.get("metrics")
    if not isinstance(metrics, dict):
        return None
    return _coerce_int(metrics.get(key))
