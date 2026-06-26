"""Resolve and cache `SKILL.md` files for the AgentSkill runner.

Workflow per request:

1. If ``force_refresh`` is false AND we have a URI→SHA mapping in
   ``agent_skills/_index.json`` AND the cached SKILL.md is still in
   storage → return the cached entry, ``cached=True``.
2. Otherwise download the URI body, compute SHA-256.
3. If the caller pinned ``expected_sha256`` and it doesn't match the
   downloaded payload → raise :class:`AgentSkillShaMismatchError`.
4. Parse the YAML frontmatter; if the file has no frontmatter or
   ``name`` is absent → raise :class:`AgentSkillManifestError`.
5. Persist the bytes at ``agent_skills/<sha256>/SKILL.md`` (skip the
   upload when the object already exists) and update
   ``agent_skills/_index.json``.

The resolver is intentionally stateless — it takes the storage service
and an httpx client as constructor args so tests can substitute fakes
without monkey-patching.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional

import httpx
import yaml
from loguru import logger

from ..models.agent_skill import (
    AgentSkillManifest,
    AgentSkillResolveRequest,
    AgentSkillResolveResponse,
)
from .storage_service import StorageService

SKILLS_PREFIX = "agent_skills/"
INDEX_OBJECT = f"{SKILLS_PREFIX}_index.json"

# Matches the leading YAML frontmatter block (`---\n...\n---\n`) at the
# very start of a SKILL.md. Tolerates CRLF + optional BOM.
_FRONTMATTER_RE = re.compile(
    r"^﻿?---\r?\n(?P<body>.*?)\r?\n---\r?\n",
    re.DOTALL,
)


class AgentSkillError(Exception):
    """Base error for the resolver."""


class AgentSkillFetchError(AgentSkillError):
    """Raised when the URI cannot be downloaded."""


class AgentSkillShaMismatchError(AgentSkillError):
    """Raised when ``expected_sha256`` is set and disagrees with the payload."""

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(f"SHA-256 pin mismatch: expected {expected}, got {actual}")
        self.expected = expected
        self.actual = actual


class AgentSkillManifestError(AgentSkillError):
    """Raised when the SKILL.md frontmatter is missing or malformed."""


def _normalise_allowed_tools(value: Any) -> List[str]:
    """Coerce the ``allowed-tools`` frontmatter value into ``list[str]``.

    Accepts both YAML list form (``[A, B]``) and the comma-separated
    string form Anthropic's own skills use (``"Read, Bash"``).
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [tok.strip() for tok in value.split(",") if tok.strip()]
    return [str(value)]


def parse_skill_md(content: bytes) -> AgentSkillManifest:
    """Parse the YAML frontmatter of a SKILL.md byte payload.

    Raises :class:`AgentSkillManifestError` for missing / unparseable
    frontmatter or when the required ``name`` field is absent.
    """
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AgentSkillManifestError(f"SKILL.md is not valid UTF-8: {exc}") from exc

    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise AgentSkillManifestError("SKILL.md is missing a YAML frontmatter block")

    try:
        loaded = yaml.safe_load(match.group("body")) or {}
    except yaml.YAMLError as exc:
        raise AgentSkillManifestError(f"SKILL.md frontmatter is not valid YAML: {exc}") from exc

    if not isinstance(loaded, dict):
        raise AgentSkillManifestError("SKILL.md frontmatter must be a YAML mapping")

    name = loaded.get("name")
    if not isinstance(name, str) or not name.strip():
        raise AgentSkillManifestError("SKILL.md frontmatter is missing required field 'name'")

    description = loaded.get("description")
    if description is not None and not isinstance(description, str):
        description = str(description)

    allowed_tools = _normalise_allowed_tools(loaded.get("allowed-tools"))

    return AgentSkillManifest(
        name=name.strip(),
        description=description.strip() if isinstance(description, str) else description,
        allowed_tools=allowed_tools,
        raw=loaded,
    )


class AgentSkillResolver:
    """Stateless helper coordinating fetch + parse + cache + index."""

    def __init__(self, storage: StorageService, http: Optional[httpx.AsyncClient] = None) -> None:
        self._storage = storage
        self._http = http  # injectable for tests; created lazily otherwise
        self._owns_http = http is None

    async def _fetch(self, uri: str) -> bytes:
        client = self._http or httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        try:
            try:
                response = await client.get(uri)
            except httpx.HTTPError as exc:
                raise AgentSkillFetchError(f"Failed to GET {uri}: {exc}") from exc
            if response.status_code >= 400:
                raise AgentSkillFetchError(
                    f"GET {uri} returned HTTP {response.status_code}"
                )
            return response.content
        finally:
            if self._owns_http:
                await client.aclose()

    async def _load_index(self) -> Dict[str, str]:
        raw = await self._storage.download_bytes(INDEX_OBJECT)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning(f"agent_skills index corrupt, treating as empty: {exc}")
            return {}
        mapping = data.get("uri_to_sha256") if isinstance(data, dict) else None
        return mapping if isinstance(mapping, dict) else {}

    async def _save_index(self, index: Dict[str, str]) -> None:
        payload = json.dumps({"uri_to_sha256": index}, indent=2, sort_keys=True).encode("utf-8")
        ok = await self._storage.upload_bytes(payload, INDEX_OBJECT, metadata={"type": "agent_skill_index"})
        if not ok:
            logger.warning("Failed to persist agent_skills index; next call will re-fetch from URI")

    def _object_key(self, sha256: str) -> str:
        return f"{SKILLS_PREFIX}{sha256}/SKILL.md"

    async def resolve(self, req: AgentSkillResolveRequest) -> AgentSkillResolveResponse:
        index = await self._load_index()

        cached_sha = None if req.force_refresh else index.get(req.uri)
        if cached_sha:
            cached_key = self._object_key(cached_sha)
            cached_bytes = await self._storage.download_bytes(cached_key)
            if cached_bytes:
                manifest = parse_skill_md(cached_bytes)
                # Cached entry MUST also satisfy the caller's SHA pin.
                if req.expected_sha256 and req.expected_sha256 != cached_sha:
                    raise AgentSkillShaMismatchError(req.expected_sha256, cached_sha)
                return AgentSkillResolveResponse(
                    skill_id=cached_sha,
                    sha256=cached_sha,
                    uri=req.uri,
                    storage_path=cached_key,
                    manifest=manifest,
                    cached=True,
                    size_bytes=len(cached_bytes),
                )

        content = await self._fetch(req.uri)
        sha256 = hashlib.sha256(content).hexdigest()
        if req.expected_sha256 and req.expected_sha256 != sha256:
            raise AgentSkillShaMismatchError(req.expected_sha256, sha256)

        manifest = parse_skill_md(content)
        storage_path = self._object_key(sha256)

        already_cached = await self._storage.object_exists(storage_path)
        if not already_cached:
            ok = await self._storage.upload_bytes(
                content,
                storage_path,
                metadata={
                    "type": "agent_skill",
                    "sha256": sha256,
                    "source_uri": req.uri,
                    "name": manifest.name,
                },
            )
            if not ok:
                raise AgentSkillError(f"Failed to persist SKILL.md to {storage_path}")

        # Update URI→SHA index (best-effort; failures don't poison the response).
        if index.get(req.uri) != sha256:
            index[req.uri] = sha256
            await self._save_index(index)

        return AgentSkillResolveResponse(
            skill_id=sha256,
            sha256=sha256,
            uri=req.uri,
            storage_path=storage_path,
            manifest=manifest,
            cached=False,
            size_bytes=len(content),
        )
