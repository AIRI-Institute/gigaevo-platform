"""Tests for ``master_api.src.services.agent_skill_resolver``.

The resolver is exercised against:
- a fake in-memory storage that mimics the ``StorageService`` async surface
  (``download_bytes`` / ``upload_bytes`` / ``object_exists``),
- an ``httpx.AsyncClient`` backed by ``MockTransport`` so the tests never
  touch the network.

We check: happy path; the URI→SHA index is written; second call returns
cached=True and does NOT re-fetch; ``force_refresh=True`` bypasses cache;
``expected_sha256`` mismatch raises; malformed/missing frontmatter
raises; fetch HTTP error raises; CRLF + BOM frontmatter still parses.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MASTER_SRC = REPO_ROOT / "master_api" / "src"

# Make ``src.*`` importable as a top-level package (the package layout the
# service uses at runtime — ``src.services.agent_skill_resolver``).
sys.path.insert(0, str(MASTER_SRC.parent))

# Lazy-import after sys.path manipulation so tests work even if the
# module was previously cached against a different path. Drop the bare
# ``src`` entry too — otherwise ``importlib.import_module('src.<x>')``
# will resolve via the cached package from the other service tree.
for _stale in [m for m in list(sys.modules) if m == "src" or m.startswith("src.")]:
    sys.modules.pop(_stale, None)

asr = importlib.import_module("src.services.agent_skill_resolver")
models = importlib.import_module("src.models.agent_skill")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeStorage:
    """In-memory stand-in for ``StorageService``."""

    def __init__(self) -> None:
        self.objects: Dict[str, bytes] = {}
        self.uploads: List[str] = []  # records every successful upload key

    async def download_bytes(self, object_name: str) -> Optional[bytes]:
        return self.objects.get(object_name)

    async def upload_bytes(self, data: bytes, object_name: str, metadata=None) -> bool:
        self.objects[object_name] = data
        self.uploads.append(object_name)
        return True

    async def object_exists(self, object_name: str) -> bool:
        return object_name in self.objects


def _mock_client(responses: Dict[str, bytes], statuses: Optional[Dict[str, int]] = None) -> httpx.AsyncClient:
    """Return an httpx.AsyncClient whose GETs are served from ``responses``."""
    statuses = statuses or {}

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in responses:
            return httpx.Response(statuses.get(url, 200), content=responses[url])
        return httpx.Response(404, content=b"not found")

    return httpx.AsyncClient(transport=httpx.MockTransport(_handler))


SAMPLE_SKILL_MD = b"""---
name: pdf-tools
description: Read and summarise PDF files.
allowed-tools:
  - Read
  - WebFetch(domain:*.adobe.com)
---
# PDF tools

Body text follows.
"""

SAMPLE_URI = "https://example.test/pdf/SKILL.md"
EXPECTED_SHA = hashlib.sha256(SAMPLE_SKILL_MD).hexdigest()
EXPECTED_KEY = f"agent_skills/{EXPECTED_SHA}/SKILL.md"


# ---------------------------------------------------------------------------
# parse_skill_md
# ---------------------------------------------------------------------------


def test_parse_skill_md_happy_path():
    manifest = asr.parse_skill_md(SAMPLE_SKILL_MD)
    assert manifest.name == "pdf-tools"
    assert manifest.description == "Read and summarise PDF files."
    assert manifest.allowed_tools == ["Read", "WebFetch(domain:*.adobe.com)"]
    # ``raw`` carries the full mapping for callers that need the rest.
    assert manifest.raw["name"] == "pdf-tools"


def test_parse_skill_md_accepts_comma_separated_allowed_tools():
    payload = b"---\nname: t\nallowed-tools: Read, Bash, Edit\n---\nbody\n"
    manifest = asr.parse_skill_md(payload)
    assert manifest.allowed_tools == ["Read", "Bash", "Edit"]


def test_parse_skill_md_tolerates_crlf_and_bom():
    payload = "﻿---\r\nname: tool\r\n---\r\nbody\r\n".encode("utf-8")
    manifest = asr.parse_skill_md(payload)
    assert manifest.name == "tool"


@pytest.mark.parametrize(
    "payload",
    [
        b"no frontmatter at all",
        b"---\n---\nbody\n",  # frontmatter present but no name
        b"---\nname:\n---\nbody\n",  # name empty
        b"---\nname: ok\n: not valid yaml: : :\n---\nbody\n",  # malformed yaml
    ],
)
def test_parse_skill_md_rejects_bad_payloads(payload):
    with pytest.raises(asr.AgentSkillManifestError):
        asr.parse_skill_md(payload)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_happy_path_writes_cache_and_index():
    storage = FakeStorage()
    http = _mock_client({SAMPLE_URI: SAMPLE_SKILL_MD})
    try:
        resolver = asr.AgentSkillResolver(storage=storage, http=http)
        resp = await resolver.resolve(models.AgentSkillResolveRequest(uri=SAMPLE_URI))

        assert resp.sha256 == EXPECTED_SHA
        assert resp.skill_id == EXPECTED_SHA
        assert resp.storage_path == EXPECTED_KEY
        assert resp.cached is False
        assert resp.size_bytes == len(SAMPLE_SKILL_MD)
        assert resp.manifest.name == "pdf-tools"
        # SKILL.md persisted at the content-addressed key.
        assert storage.objects[EXPECTED_KEY] == SAMPLE_SKILL_MD
        # URI→SHA index written.
        index = json.loads(storage.objects[asr.INDEX_OBJECT].decode("utf-8"))
        assert index == {"uri_to_sha256": {SAMPLE_URI: EXPECTED_SHA}}
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_second_resolve_hits_cache_and_skips_fetch():
    storage = FakeStorage()
    fetch_count = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        fetch_count["n"] += 1
        return httpx.Response(200, content=SAMPLE_SKILL_MD)

    http = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    try:
        resolver = asr.AgentSkillResolver(storage=storage, http=http)
        # First call populates cache (1 fetch).
        await resolver.resolve(models.AgentSkillResolveRequest(uri=SAMPLE_URI))
        # Second call: must serve from cache, fetch_count stays at 1.
        second = await resolver.resolve(models.AgentSkillResolveRequest(uri=SAMPLE_URI))
        assert second.cached is True
        assert second.sha256 == EXPECTED_SHA
        assert fetch_count["n"] == 1
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_force_refresh_bypasses_cache():
    storage = FakeStorage()
    fetch_count = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        fetch_count["n"] += 1
        return httpx.Response(200, content=SAMPLE_SKILL_MD)

    http = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    try:
        resolver = asr.AgentSkillResolver(storage=storage, http=http)
        await resolver.resolve(models.AgentSkillResolveRequest(uri=SAMPLE_URI))
        forced = await resolver.resolve(
            models.AgentSkillResolveRequest(uri=SAMPLE_URI, force_refresh=True)
        )
        assert forced.cached is False
        assert fetch_count["n"] == 2
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_expected_sha_mismatch_raises_with_actual():
    storage = FakeStorage()
    http = _mock_client({SAMPLE_URI: SAMPLE_SKILL_MD})
    try:
        resolver = asr.AgentSkillResolver(storage=storage, http=http)
        wrong = "deadbeef" * 8
        with pytest.raises(asr.AgentSkillShaMismatchError) as exc:
            await resolver.resolve(
                models.AgentSkillResolveRequest(uri=SAMPLE_URI, expected_sha256=wrong)
            )
        assert exc.value.expected == wrong
        assert exc.value.actual == EXPECTED_SHA
        # Pin mismatch must NOT poison the cache.
        assert EXPECTED_KEY not in storage.objects
        assert asr.INDEX_OBJECT not in storage.objects
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_expected_sha_matches_when_correct():
    storage = FakeStorage()
    http = _mock_client({SAMPLE_URI: SAMPLE_SKILL_MD})
    try:
        resolver = asr.AgentSkillResolver(storage=storage, http=http)
        resp = await resolver.resolve(
            models.AgentSkillResolveRequest(uri=SAMPLE_URI, expected_sha256=EXPECTED_SHA)
        )
        assert resp.sha256 == EXPECTED_SHA
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_fetch_failure_raises_fetch_error():
    storage = FakeStorage()
    http = _mock_client({}, statuses={SAMPLE_URI: 500})
    try:
        resolver = asr.AgentSkillResolver(storage=storage, http=http)
        with pytest.raises(asr.AgentSkillFetchError):
            await resolver.resolve(models.AgentSkillResolveRequest(uri=SAMPLE_URI))
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_bad_frontmatter_raises_and_does_not_cache():
    storage = FakeStorage()
    bad = b"no frontmatter here\nbody only\n"
    http = _mock_client({SAMPLE_URI: bad})
    try:
        resolver = asr.AgentSkillResolver(storage=storage, http=http)
        with pytest.raises(asr.AgentSkillManifestError):
            await resolver.resolve(models.AgentSkillResolveRequest(uri=SAMPLE_URI))
        bad_sha = hashlib.sha256(bad).hexdigest()
        assert f"agent_skills/{bad_sha}/SKILL.md" not in storage.objects
        assert asr.INDEX_OBJECT not in storage.objects
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_cache_pin_mismatch_does_not_serve_stale():
    """Cached entry should still be rejected when caller pins a different SHA."""
    storage = FakeStorage()
    http = _mock_client({SAMPLE_URI: SAMPLE_SKILL_MD})
    try:
        resolver = asr.AgentSkillResolver(storage=storage, http=http)
        await resolver.resolve(models.AgentSkillResolveRequest(uri=SAMPLE_URI))
        # Second call asks for a different SHA on the SAME uri → must raise.
        with pytest.raises(asr.AgentSkillShaMismatchError):
            await resolver.resolve(
                models.AgentSkillResolveRequest(uri=SAMPLE_URI, expected_sha256="0" * 64)
            )
    finally:
        await http.aclose()
