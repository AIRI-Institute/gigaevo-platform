"""Tests for the platform↔Memory forwarding path (CARE §4.6).

Covers:

* :class:`MemoryClient` over ``httpx.MockTransport`` — happy path,
  network errors, non-2xx, missing entity_id, unconfigured base url.
* :meth:`EvolutionService._evolution_meta` shape matches the
  PREPARE.md §1.6 contract.
* `record_individual` forwards to Memory once configured, with the
  correct ``content`` + ``evolution_meta`` payload, and **does not
  break** when Memory raises / returns 500.
* `accept_individual` writes the chain to the ``stable`` channel.
* Skipping behaviour — when ``MEMORY_API_URL`` is unset, no Memory
  calls are made.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MASTER_SRC = REPO_ROOT / "master_api" / "src"

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(MASTER_SRC.parent))

for _stale in [m for m in list(sys.modules) if m == "src" or m.startswith("src.")]:
    sys.modules.pop(_stale, None)

models = importlib.import_module("src.models.evolution")
svc_mod = importlib.import_module("src.services.evolution_service")
bus_mod = importlib.import_module("src.services.evolution_event_bus")
memory_client_mod = importlib.import_module("src.services.memory_client")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeStorage:
    def __init__(self) -> None:
        self.objects: Dict[str, bytes] = {}
        self.metadata: Dict[str, Optional[Dict[str, str]]] = {}

    async def download_bytes(self, k: str) -> Optional[bytes]:
        return self.objects.get(k)

    async def upload_bytes(self, data: bytes, k: str, metadata=None) -> bool:
        self.objects[k] = data
        self.metadata[k] = metadata
        return True

    async def object_exists(self, k: str) -> bool:
        return k in self.objects

    async def list_objects(self, prefix: str = "", recursive: bool = False) -> List[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))


def _evo_payload(objectives=("accuracy",)) -> Dict[str, Any]:
    return {
        "name": "evo-memory-test",
        "description": "checking the Memory integration",
        "seed_chains": [{"chain_content": {"name": "seed", "template": "echo"}}],
        "fitness": {"prompt": "Is the answer correct?"},
        "objectives": list(objectives),
        "ga_config": {"population_size": 2, "max_iterations": 3},
        "tags": ["demo"],
    }


def _mock_memory_client(
    handler, base_url: str = "http://memory.test:8002"
) -> memory_client_mod.MemoryClient:
    """Return a MemoryClient backed by ``httpx.MockTransport(handler)``."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return memory_client_mod.MemoryClient(base_url, client=client)


# ---------------------------------------------------------------------------
# MemoryClient
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_client_unconfigured_is_a_noop():
    client = memory_client_mod.MemoryClient(base_url=None)
    assert client.is_configured is False
    assert (
        await client.save_chain(
            name="x", content={}, evolution_meta={}, tags=[], description=None
        )
        is None
    )
    assert await client.set_channel("e-1", channel="stable") is False


@pytest.mark.asyncio
async def test_memory_client_save_chain_happy_path():
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url), json.loads(request.content)))
        return httpx.Response(201, json={"entity_id": "ent-42", "version_id": "v1"})

    client = _mock_memory_client(handler)
    entity_id = await client.save_chain(
        name="weather#gen0",
        content={"node": "echo"},
        evolution_meta={"generation": 0, "fitness_score": 0.7},
        tags=["demo"],
        description="desc",
        channel="latest",
    )
    assert entity_id == "ent-42"
    assert len(seen) == 1
    method, url, body = seen[0]
    assert method == "POST"
    assert url == "http://memory.test:8002/v1/chains"
    assert body["meta"]["name"] == "weather#gen0"
    assert body["meta"]["tags"] == ["demo"]
    assert body["meta"]["when_to_use"] == "desc"
    assert body["content"] == {"node": "echo"}
    assert body["evolution_meta"]["fitness_score"] == 0.7
    assert body["channel"] == "latest"


@pytest.mark.asyncio
async def test_memory_client_set_channel_happy_path():
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={"ok": True})

    client = _mock_memory_client(handler)
    assert await client.set_channel("ent-7", channel="stable") is True
    method, url, body = seen[0]
    assert method == "PATCH"
    assert url == "http://memory.test:8002/v1/chains/ent-7/channel"
    assert body == {"channel": "stable"}


@pytest.mark.asyncio
async def test_memory_client_returns_none_on_5xx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    client = _mock_memory_client(handler)
    assert (
        await client.save_chain(name="x", content={}, evolution_meta={}) is None
    )
    assert await client.set_channel("e", channel="stable") is False


@pytest.mark.asyncio
async def test_memory_client_returns_none_when_body_missing_entity_id():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"version_id": "v1"})

    client = _mock_memory_client(handler)
    assert (
        await client.save_chain(name="x", content={}, evolution_meta={}) is None
    )


@pytest.mark.asyncio
async def test_memory_client_swallows_network_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    client = _mock_memory_client(handler)
    assert (
        await client.save_chain(name="x", content={}, evolution_meta={}) is None
    )
    assert await client.set_channel("e", channel="stable") is False


@pytest.mark.asyncio
async def test_memory_client_accepts_alt_id_key():
    """Some Memory builds return `id` instead of `entity_id`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": "alt-id"})

    client = _mock_memory_client(handler)
    eid = await client.save_chain(name="x", content={}, evolution_meta={})
    assert eid == "alt-id"


# ---------------------------------------------------------------------------
# _evolution_meta shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evolution_meta_matches_prepare_schema():
    storage = FakeStorage()
    bus = bus_mod.EvolutionEventBus()
    service = svc_mod.EvolutionService(storage=storage, event_bus=bus)
    evo = await service.create_evolution(
        models.EvolutionCreate.model_validate(_evo_payload(objectives=("accuracy", "tpt")))
    )
    ind = await service.record_individual(
        evo.evolution_id,
        models.IndividualCreate.model_validate(
            {
                "generation": 3,
                "chain_content": {"x": 1},
                "fitness_scores": {"accuracy": 0.8, "tpt": 0.4},
                "parent_ids": ["p-1", "p-2"],
                "mutation_kind": "crossover",
            }
        ),
    )
    meta = service._evolution_meta(evo, ind, primary_objective="accuracy")
    assert meta == {
        "parent_version_ids": ["p-1", "p-2"],
        "fitness_score": 0.8,
        "generation": 3,
        "experiment_id": evo.evolution_id,
        "objectives": {"accuracy": 0.8, "tpt": 0.4},
        "mutation_kind": "crossover",
    }


# ---------------------------------------------------------------------------
# EvolutionService → MemoryClient integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_individual_forwards_to_memory_latest_channel():
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url), json.loads(request.content)))
        return httpx.Response(201, json={"entity_id": "ent-x"})

    memory = _mock_memory_client(handler)
    service = svc_mod.EvolutionService(
        storage=FakeStorage(), event_bus=bus_mod.EvolutionEventBus(), memory_client=memory
    )
    evo = await service.create_evolution(models.EvolutionCreate.model_validate(_evo_payload()))
    await service.record_individual(
        evo.evolution_id,
        models.IndividualCreate.model_validate(
            {
                "generation": 0,
                "chain_content": {"a": 1},
                "fitness_scores": {"accuracy": 0.5},
                "parent_ids": [],
                "mutation_kind": "seed",
            }
        ),
    )

    save_calls = [c for c in calls if c[0] == "POST"]
    assert len(save_calls) == 1
    _, url, body = save_calls[0]
    assert url.endswith("/v1/chains")
    assert body["channel"] == "latest"
    assert body["content"] == {"a": 1}
    assert body["evolution_meta"]["fitness_score"] == 0.5
    assert body["evolution_meta"]["experiment_id"] == evo.evolution_id
    assert body["evolution_meta"]["mutation_kind"] == "seed"
    assert body["meta"]["name"].endswith("#gen0")


@pytest.mark.asyncio
async def test_record_individual_does_not_break_on_memory_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"memory unavailable")

    memory = _mock_memory_client(handler)
    service = svc_mod.EvolutionService(
        storage=FakeStorage(), event_bus=bus_mod.EvolutionEventBus(), memory_client=memory
    )
    evo = await service.create_evolution(models.EvolutionCreate.model_validate(_evo_payload()))
    # Must succeed despite Memory returning 503.
    ind = await service.record_individual(
        evo.evolution_id,
        models.IndividualCreate.model_validate(
            {"generation": 0, "chain_content": {}, "fitness_scores": {"accuracy": 0.1}}
        ),
    )
    assert ind.individual_id  # platform-side persistence still happened
    # And the evolution record was patched normally.
    refreshed = await service.get_evolution(evo.evolution_id)
    assert refreshed.best_individual_id == ind.individual_id


@pytest.mark.asyncio
async def test_record_individual_skips_memory_when_unconfigured():
    """No client → no calls. Existing behaviour preserved verbatim."""
    service = svc_mod.EvolutionService(
        storage=FakeStorage(), event_bus=bus_mod.EvolutionEventBus(), memory_client=None
    )
    evo = await service.create_evolution(models.EvolutionCreate.model_validate(_evo_payload()))
    ind = await service.record_individual(
        evo.evolution_id,
        models.IndividualCreate.model_validate(
            {"generation": 0, "chain_content": {}, "fitness_scores": {"accuracy": 0.1}}
        ),
    )
    # No exception, no assertion failure. The fact that the test reaches
    # this point IS the assertion — nothing tried to call Memory.
    assert ind.individual_id


@pytest.mark.asyncio
async def test_memory_client_promote_evolved_chain_happy_path():
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, str(request.url), body, dict(request.headers)))
        if request.method == "GET" and request.url.path.endswith("/v1/chains/chain-1"):
            return httpx.Response(
                200,
                json={
                    "entity_id": "chain-1",
                    "version_id": "ver-4",
                    "version_number": 4,
                    "etag": "etag-4",
                    "meta": {
                        "name": "Existing chain",
                        "tags": ["user-tag"],
                        "when_to_use": "original",
                    },
                },
            )
        if request.method == "GET" and request.url.path.endswith("/v1/chains/chain-1/versions"):
            return httpx.Response(
                200,
                json=[
                    {
                        "version_id": "ver-4",
                        "version_number": 4,
                        "evolution_meta": {"metrics": {"acceptance_id": "other"}},
                    }
                ],
            )
        return httpx.Response(
            200,
            json={"entity_id": "chain-1", "version_id": "ver-5", "version_number": 5},
        )

    memory = _mock_memory_client(handler)
    result = await memory.promote_evolved_chain(
        chain_id="chain-1",
        content={"a": 2},
        evolution_meta={
            "fitness_score": 0.7,
            "objectives": {"accuracy": 0.7},
            "metrics": {"acceptance_id": "evo-1:ind-1:chain-1"},
        },
        name="accepted chain",
        tags=["demo"],
        description="desc",
    )

    assert result.chain_id == "chain-1"
    assert result.previous_version == 4
    assert result.new_version == 5
    assert result.new_version_id == "ver-5"
    assert calls[0][0] == "GET"
    assert calls[0][1] == "http://memory.test:8002/v1/chains/chain-1?channel=latest"
    assert calls[1][0] == "GET"
    assert calls[1][1] == "http://memory.test:8002/v1/chains/chain-1/versions?limit=1"
    assert calls[2][0] == "PUT"
    assert calls[2][1] == "http://memory.test:8002/v1/chains/chain-1"
    assert calls[2][3]["if-match"] == "etag-4"
    assert calls[2][2]["parent_version_id"] == "ver-4"
    assert calls[2][2]["channel"] == "latest"
    assert calls[2][2]["content"] == {"a": 2}
    assert calls[2][2]["meta"] == {
        "name": "Existing chain",
        "tags": ["user-tag"],
        "when_to_use": "original",
    }
    assert calls[2][2]["evolution_meta"]["metrics"]["evolved_from_version"] == 4


@pytest.mark.asyncio
async def test_memory_client_promote_evolved_chain_is_idempotent_for_same_acceptance():
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.url.path.endswith("/v1/chains/chain-1/versions"):
            return httpx.Response(
                200,
                json=[
                    {
                        "version_id": "ver-5",
                        "version_number": 5,
                        "evolution_meta": {
                            "metrics": {
                                "acceptance_id": "evo-1:ind-1:chain-1",
                                "evolved_from_version": 4,
                            }
                        },
                    }
                ],
            )
        return httpx.Response(
            200,
            json={
                "entity_id": "chain-1",
                "version_id": "ver-5",
                "version_number": 5,
                "etag": "etag-5",
            },
        )

    memory = _mock_memory_client(handler)
    result = await memory.promote_evolved_chain(
        chain_id="chain-1",
        content={"a": 2},
        evolution_meta={"metrics": {"acceptance_id": "evo-1:ind-1:chain-1"}},
        name="accepted chain",
    )

    assert result.previous_version == 4
    assert result.new_version == 5
    assert result.new_version_id == "ver-5"
    assert calls == ["GET", "GET"]


@pytest.mark.asyncio
async def test_accept_individual_promotes_memory_backed_chain_strictly():
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, str(request.url), body, dict(request.headers)))
        if request.method == "GET" and request.url.path.endswith("/v1/chains/chain-1"):
            return httpx.Response(
                200,
                json={
                    "entity_id": "chain-1",
                    "version_id": "ver-4",
                    "version_number": 4,
                    "etag": "etag-4",
                    "meta": {
                        "name": "Existing chain",
                        "tags": ["user-tag"],
                        "when_to_use": "original",
                    },
                },
            )
        if request.method == "GET" and request.url.path.endswith("/v1/chains/chain-1/versions"):
            return httpx.Response(200, json=[])
        if request.method == "PUT" and request.url.path.endswith("/v1/chains/chain-1"):
            return httpx.Response(
                200,
                json={"entity_id": "chain-1", "version_id": "ver-5", "version_number": 5},
            )
        return httpx.Response(201, json={"entity_id": "ind-temp"})

    memory = _mock_memory_client(handler)
    service = svc_mod.EvolutionService(
        storage=FakeStorage(), event_bus=bus_mod.EvolutionEventBus(), memory_client=memory
    )
    payload = _evo_payload()
    payload["seed_chains"] = [{"memory_chain_id": "chain-1"}]
    evo = await service.create_evolution(models.EvolutionCreate.model_validate(payload))
    ind = await service.record_individual(
        evo.evolution_id,
        models.IndividualCreate.model_validate(
            {"generation": 0, "chain_content": {"a": 2}, "fitness_scores": {"accuracy": 0.7}}
        ),
    )
    after = await service.accept_individual(evo.evolution_id, ind.individual_id, note="best")

    assert after.status == models.EvolutionStatus.ACCEPTED
    assert after.accepted_chain_id == "chain-1"
    assert after.previous_version == 4
    assert after.new_version == 5
    assert after.new_version_id == "ver-5"
    strict_payload = next(
        body for method, _url, body, _headers in calls
        if method == "PUT"
    )
    assert strict_payload["channel"] == "latest"
    assert strict_payload["content"] == {"a": 2}
    assert strict_payload["meta"]["name"] == "Existing chain"
    assert strict_payload["meta"]["tags"] == ["user-tag"]
    assert strict_payload["parent_version_id"] == "ver-4"
    assert strict_payload["evolution_meta"]["experiment_id"] == evo.evolution_id
    assert strict_payload["evolution_meta"]["objectives"] == {"accuracy": 0.7}
    assert strict_payload["evolution_meta"]["metrics"]["acceptance_id"] == (
        f"{evo.evolution_id}:{ind.individual_id}:chain-1"
    )
    assert strict_payload["evolution_meta"]["metrics"]["accepted_at"]
    assert strict_payload["evolution_meta"]["metrics"]["evolved_from_version"] == 4


@pytest.mark.asyncio
async def test_accept_individual_rejects_unaffected_chain_override():
    memory = _mock_memory_client(lambda _request: httpx.Response(500, content=b"boom"))
    service = svc_mod.EvolutionService(
        storage=FakeStorage(), event_bus=bus_mod.EvolutionEventBus(), memory_client=memory
    )
    payload = _evo_payload()
    payload["seed_chains"] = [{"memory_chain_id": "chain-1"}]
    evo = await service.create_evolution(models.EvolutionCreate.model_validate(payload))
    ind = await service.record_individual(
        evo.evolution_id,
        models.IndividualCreate.model_validate(
            {"generation": 0, "chain_content": {"a": 2}, "fitness_scores": {"accuracy": 0.7}}
        ),
    )

    with pytest.raises(svc_mod.EvolutionAcceptError):
        await service.accept_individual(
            evo.evolution_id,
            ind.individual_id,
            chain_id_to_update="other-chain",
        )


@pytest.mark.asyncio
async def test_accept_individual_uses_seed_memory_ids_for_legacy_records():
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, str(request.url), body))
        if request.method == "GET" and request.url.path.endswith("/v1/chains/chain-legacy"):
            return httpx.Response(
                200,
                json={
                    "entity_id": "chain-legacy",
                    "version_id": "ver-1",
                    "version_number": 1,
                    "etag": "etag-1",
                    "meta": {"name": "Legacy chain", "tags": []},
                },
            )
        if request.method == "GET" and request.url.path.endswith("/v1/chains/chain-legacy/versions"):
            return httpx.Response(200, json=[])
        if request.method == "PUT":
            return httpx.Response(
                200,
                json={
                    "entity_id": "chain-legacy",
                    "version_id": "ver-2",
                    "version_number": 2,
                },
            )
        return httpx.Response(201, json={"entity_id": "ind-temp"})

    memory = _mock_memory_client(handler)
    service = svc_mod.EvolutionService(
        storage=FakeStorage(), event_bus=bus_mod.EvolutionEventBus(), memory_client=memory
    )
    payload = _evo_payload()
    payload["seed_chains"] = [{"memory_chain_id": "chain-legacy"}]
    evo = await service.create_evolution(models.EvolutionCreate.model_validate(payload))
    await service._persist(evo.model_copy(update={"affected_chain_ids": []}))
    ind = await service.record_individual(
        evo.evolution_id,
        models.IndividualCreate.model_validate(
            {"generation": 0, "chain_content": {"a": 3}, "fitness_scores": {"accuracy": 0.8}}
        ),
    )

    after = await service.accept_individual(evo.evolution_id, ind.individual_id)

    assert after.accepted_chain_id == "chain-legacy"
    assert any(method == "PUT" for method, _url, _body in calls)


@pytest.mark.asyncio
async def test_accept_individual_without_affected_chain_stays_platform_local():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    memory = _mock_memory_client(handler)
    service = svc_mod.EvolutionService(
        storage=FakeStorage(), event_bus=bus_mod.EvolutionEventBus(), memory_client=memory
    )
    evo = await service.create_evolution(models.EvolutionCreate.model_validate(_evo_payload()))
    ind = await service.record_individual(
        evo.evolution_id,
        models.IndividualCreate.model_validate(
            {"generation": 0, "chain_content": {}, "fitness_scores": {"accuracy": 0.5}}
        ),
    )
    # Accept must NOT raise even when both Memory calls fail.
    after = await service.accept_individual(evo.evolution_id, ind.individual_id)
    assert after.status == models.EvolutionStatus.ACCEPTED
    assert after.accepted_individual_id == ind.individual_id


@pytest.mark.asyncio
async def test_accept_individual_memory_backed_failure_leaves_state_unchanged():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(503, content=b"memory unavailable")
        return httpx.Response(201, json={"entity_id": "ind-temp"})

    memory = _mock_memory_client(handler)
    storage = FakeStorage()
    service = svc_mod.EvolutionService(
        storage=storage, event_bus=bus_mod.EvolutionEventBus(), memory_client=memory
    )
    payload = _evo_payload()
    payload["seed_chains"] = [{"memory_chain_id": "chain-1"}]
    evo = await service.create_evolution(models.EvolutionCreate.model_validate(payload))
    ind = await service.record_individual(
        evo.evolution_id,
        models.IndividualCreate.model_validate(
            {"generation": 0, "chain_content": {}, "fitness_scores": {"accuracy": 0.5}}
        ),
    )

    with pytest.raises(svc_mod.EvolutionAcceptError):
        await service.accept_individual(evo.evolution_id, ind.individual_id)

    refreshed = await service.get_evolution(evo.evolution_id)
    refreshed_ind = await service.get_individual(evo.evolution_id, ind.individual_id)
    assert refreshed.status == models.EvolutionStatus.QUEUED
    assert refreshed.accepted_individual_id is None
    assert refreshed_ind.accepted is False
