"""Tests for the evolution API (CARE §4.1).

Coverage:

* Pydantic schemas — seed_chain XOR (inline content / Memory ref),
  objectives uniqueness + non-empty, GA caps.
* ``EvolutionService.create_evolution`` — persists JSON to MinIO under
  ``evolutions/<id>.json`` and returns a hydratable record.
* ``EvolutionService.get_evolution`` — round-trips through the fake
  storage and reconstructs the same record.
* ``update_status`` — mutates only the fields you ask it to and
  bumps ``updated_at``.
* Real-app smoke via TestClient against the fully-wired master_api
  FastAPI app (auth on, fake storage swapped in). Covers happy POST
  (201 + record body), 422 on malformed payload, 401 without auth,
  POST → GET round-trip, 404 on unknown id.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MASTER_SRC = REPO_ROOT / "master_api" / "src"

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(MASTER_SRC.parent))

for _stale in [m for m in list(sys.modules) if m == "src" or m.startswith("src.")]:
    sys.modules.pop(_stale, None)

models = importlib.import_module("src.models.evolution")
svc_mod = importlib.import_module("src.services.evolution_service")


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


def _inline_seed(template: str = "echo {input}") -> Dict[str, Any]:
    return {"chain_content": {"name": "seed", "template": template}}


def _good_payload(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "name": "weather-bot",
        "description": "Evolve a chain that summarises weather",
        "seed_chains": [_inline_seed()],
        "fitness": {"prompt": "Does the response answer the question?", "higher_is_better": True},
        "objectives": ["accuracy"],
        "ga_config": {"population_size": 4, "max_iterations": 10},
        "tags": ["demo"],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_evolution_create_happy_path():
    ec = models.EvolutionCreate.model_validate(_good_payload())
    assert ec.name == "weather-bot"
    assert ec.ga_config.population_size == 4
    assert ec.objectives == ["accuracy"]
    assert ec.seed_chains[0].chain_content == {"name": "seed", "template": "echo {input}"}


def test_seed_chain_requires_exactly_one_source():
    # Both inline + memory id → reject.
    bad_both = _good_payload(
        seed_chains=[{"memory_chain_id": "abc", "chain_content": {"x": 1}}]
    )
    with pytest.raises(ValueError):
        models.EvolutionCreate.model_validate(bad_both)
    # Neither → reject.
    bad_none = _good_payload(seed_chains=[{}])
    with pytest.raises(ValueError):
        models.EvolutionCreate.model_validate(bad_none)


def test_seed_chain_memory_id_only_is_accepted():
    payload = _good_payload(seed_chains=[{"memory_chain_id": "chain-123"}])
    ec = models.EvolutionCreate.model_validate(payload)
    assert ec.seed_chains[0].memory_chain_id == "chain-123"
    assert ec.seed_chains[0].chain_content is None


def test_care_launch_payload_normalises_to_canonical_create():
    launch = models.CareEvolutionLaunch.model_validate(
        {
            "base_chain_id": "chain-123",
            "max_iterations": 12,
            "population_size": 6,
            "validation_criteria": "Prefer concise accurate answers",
            "test_data_path": "/tmp/eval.jsonl",
            "validation_threshold": 0.85,
            "evolution_mode": "per_step",
            "constraints": {"max_cost": 5},
            "extras": {"source": "modal"},
            "objectives": ["accuracy", "latency"],
            "tags": ["source:care"],
        }
    )
    ec = launch.to_evolution_create()
    assert ec.seed_chains[0].memory_chain_id == "chain-123"
    assert ec.fitness.prompt == "Prefer concise accurate answers"
    assert ec.ga_config.max_iterations == 12
    assert ec.ga_config.population_size == 6
    assert ec.objectives == ["accuracy", "latency"]
    assert ec.tags == ["source:care"]
    assert ec.launch_config == {
        "base_chain_id": "chain-123",
        "evolution_mode": "per_step",
        "test_data_path": "/tmp/eval.jsonl",
        "validation_threshold": 0.85,
        "constraints": {"max_cost": 5},
        "extras": {"source": "modal"},
    }


@pytest.mark.parametrize(
    "objectives",
    [
        [],  # empty → reject (default kicks in only when key absent)
        ["accuracy", "accuracy"],  # duplicates → reject
        ["   "],  # whitespace-only → reject after strip
    ],
)
def test_evolution_create_rejects_bad_objectives(objectives):
    payload = _good_payload(objectives=objectives)
    with pytest.raises(ValueError):
        models.EvolutionCreate.model_validate(payload)


@pytest.mark.parametrize(
    "ga_config",
    [
        {"population_size": 0},  # < 1
        {"population_size": 1000},  # > 256
        {"max_iterations": 0},
        {"mutation_rate": 1.5},
        {"mutation_rate": -0.1},
        {"crossover_rate": 2},
    ],
)
def test_ga_config_caps_enforced(ga_config):
    payload = _good_payload(ga_config=ga_config)
    with pytest.raises(ValueError):
        models.EvolutionCreate.model_validate(payload)


def test_seed_chains_minimum_and_maximum_enforced():
    # min 1
    with pytest.raises(ValueError):
        models.EvolutionCreate.model_validate(_good_payload(seed_chains=[]))
    # max 64
    too_many: List[Dict[str, Any]] = [_inline_seed() for _ in range(65)]
    with pytest.raises(ValueError):
        models.EvolutionCreate.model_validate(_good_payload(seed_chains=too_many))


# ---------------------------------------------------------------------------
# EvolutionService persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_persists_record_to_storage():
    storage = FakeStorage()
    service = svc_mod.EvolutionService(storage=storage)
    payload = models.EvolutionCreate.model_validate(_good_payload())
    record = await service.create_evolution(payload)

    assert record.evolution_id  # uuid was generated
    assert record.status == models.EvolutionStatus.QUEUED
    assert record.affected_chain_ids == []
    # Stored at the expected key.
    key = f"evolutions/{record.evolution_id}.json"
    assert key in storage.objects
    raw = storage.objects[key]
    persisted = json.loads(raw.decode("utf-8"))
    assert persisted["evolution_id"] == record.evolution_id
    assert persisted["name"] == "weather-bot"
    # Metadata exposes the status for cheap S3 LIST filters.
    assert storage.metadata[key] == {"type": "evolution_record", "status": "queued"}


@pytest.mark.asyncio
async def test_create_records_affected_chain_ids_for_memory_seeds():
    storage = FakeStorage()
    service = svc_mod.EvolutionService(storage=storage)
    payload = models.EvolutionCreate.model_validate(
        _good_payload(
            seed_chains=[
                {"memory_chain_id": "chain-a"},
                {"memory_chain_id": "chain-b"},
            ]
        )
    )
    record = await service.create_evolution(payload)
    assert record.affected_chain_ids == ["chain-a", "chain-b"]
    persisted = json.loads(storage.objects[f"evolutions/{record.evolution_id}.json"])
    assert persisted["affected_chain_ids"] == ["chain-a", "chain-b"]


@pytest.mark.asyncio
async def test_get_round_trips_record():
    storage = FakeStorage()
    service = svc_mod.EvolutionService(storage=storage)
    payload = models.EvolutionCreate.model_validate(_good_payload())
    created = await service.create_evolution(payload)

    fetched = await service.get_evolution(created.evolution_id)
    assert fetched.evolution_id == created.evolution_id
    assert fetched.name == created.name
    assert fetched.fitness.prompt == created.fitness.prompt
    assert fetched.ga_config.population_size == created.ga_config.population_size


@pytest.mark.asyncio
async def test_get_missing_id_raises_not_found():
    storage = FakeStorage()
    service = svc_mod.EvolutionService(storage=storage)
    with pytest.raises(svc_mod.EvolutionNotFoundError):
        await service.get_evolution("does-not-exist")


@pytest.mark.asyncio
async def test_get_corrupt_record_raises_persist_error():
    storage = FakeStorage()
    storage.objects["evolutions/bad-id.json"] = b"not valid json{"
    service = svc_mod.EvolutionService(storage=storage)
    with pytest.raises(svc_mod.EvolutionPersistError):
        await service.get_evolution("bad-id")


@pytest.mark.asyncio
async def test_update_status_mutates_only_supplied_fields():
    storage = FakeStorage()
    service = svc_mod.EvolutionService(storage=storage)
    payload = models.EvolutionCreate.model_validate(_good_payload())
    created = await service.create_evolution(payload)
    original_updated = created.updated_at

    patched = await service.update_status(
        created.evolution_id,
        models.EvolutionStatus.RUNNING,
        current_generation=3,
    )
    assert patched.status == models.EvolutionStatus.RUNNING
    assert patched.current_generation == 3
    # Field we didn't ask to mutate is unchanged.
    assert patched.best_individual_id is None
    # updated_at moved forward.
    assert patched.updated_at >= original_updated


@pytest.mark.asyncio
async def test_create_propagates_storage_failure():
    class FailingStorage(FakeStorage):
        async def upload_bytes(self, data, k, metadata=None):
            return False

    service = svc_mod.EvolutionService(storage=FailingStorage())
    payload = models.EvolutionCreate.model_validate(_good_payload())
    with pytest.raises(svc_mod.EvolutionPersistError):
        await service.create_evolution(payload)


# ---------------------------------------------------------------------------
# Real-app TestClient smoke
# ---------------------------------------------------------------------------


@pytest.fixture
def real_app(monkeypatch):
    """Boot the master_api app with auth on + a fake storage injected."""
    monkeypatch.setenv("KAFKA__ENABLED", "false")
    monkeypatch.setenv("DATABASE__URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("MASTER_API_KEY", "smoke-key")
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)

    # Drop cached src.* before re-importing main so env env vars take effect.
    for _stale in [m for m in list(sys.modules) if m == "src" or m.startswith("src.")]:
        sys.modules.pop(_stale, None)
    # Re-prepend master_api/ at index 0 so a sibling test file that put
    # runner_api/ there during its module-level imports doesn't make us
    # resolve ``src`` to the wrong tree.
    sys.path.insert(0, str(MASTER_SRC.parent))

    main_mod = importlib.import_module("src.main")
    evolutions_route = importlib.import_module("src.api.routes.evolutions")

    fake_storage = FakeStorage()

    class FakeServiceManager:
        def get_storage_service(self):
            return fake_storage

    evolutions_route.set_service_manager(FakeServiceManager())
    return main_mod.app, fake_storage


def test_smoke_post_requires_auth(real_app):
    from fastapi.testclient import TestClient

    app, _ = real_app
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/v1/evolutions", json=_good_payload())
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "X-API-Key"


def test_smoke_post_happy_path_returns_201_record(real_app):
    from fastapi.testclient import TestClient

    app, storage = real_app
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/api/v1/evolutions",
        headers={"X-API-Key": "smoke-key"},
        json=_good_payload(),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "weather-bot"
    assert body["status"] == "queued"
    assert body["evolution_id"]
    # And the record really landed in storage.
    assert f"evolutions/{body['evolution_id']}.json" in storage.objects


def test_smoke_post_then_get_round_trip(real_app):
    from fastapi.testclient import TestClient

    app, _ = real_app
    client = TestClient(app, raise_server_exceptions=False)
    create = client.post(
        "/api/v1/evolutions",
        headers={"X-API-Key": "smoke-key"},
        json=_good_payload(),
    )
    assert create.status_code == 201, create.text
    eid = create.json()["evolution_id"]

    fetch = client.get(f"/api/v1/evolutions/{eid}", headers={"X-API-Key": "smoke-key"})
    assert fetch.status_code == 200, fetch.text
    assert fetch.json()["evolution_id"] == eid
    assert fetch.json()["name"] == "weather-bot"


def test_smoke_post_accepts_care_launch_payload(real_app):
    from fastapi.testclient import TestClient

    app, _ = real_app
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/api/v1/evolutions",
        headers={"X-API-Key": "smoke-key"},
        json={
            "base_chain_id": "chain-care-1",
            "max_iterations": 9,
            "population_size": 5,
            "validation_criteria": "Improve reliability",
            "test_data_path": "/tmp/eval.jsonl",
            "validation_threshold": 0.9,
            "objectives": ["fitness"],
            "tags": ["source:care"],
            "constraints": {"wall_time_seconds": 120},
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["affected_chain_ids"] == ["chain-care-1"]
    assert body["seed_chains"][0]["memory_chain_id"] == "chain-care-1"
    assert body["fitness"]["prompt"] == "Improve reliability"
    assert body["ga_config"]["max_iterations"] == 9
    assert body["launch_config"]["test_data_path"] == "/tmp/eval.jsonl"
    assert body["launch_config"]["validation_threshold"] == 0.9
    assert body["launch_config"]["constraints"] == {"wall_time_seconds": 120}


def test_smoke_get_unknown_id_returns_404(real_app):
    from fastapi.testclient import TestClient

    app, _ = real_app
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get(
        "/api/v1/evolutions/does-not-exist",
        headers={"X-API-Key": "smoke-key"},
    )
    assert resp.status_code == 404


def test_smoke_post_malformed_payload_returns_422(real_app):
    from fastapi.testclient import TestClient

    app, _ = real_app
    client = TestClient(app, raise_server_exceptions=False)
    bad = _good_payload(objectives=["accuracy", "accuracy"])
    resp = client.post(
        "/api/v1/evolutions",
        headers={"X-API-Key": "smoke-key"},
        json=bad,
    )
    assert resp.status_code == 422
