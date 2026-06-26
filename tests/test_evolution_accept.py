"""Tests for ``POST /api/v1/evolutions/{id}/accept`` (CARE §4.4).

Three layers:

* Schema — ``AcceptIndividualRequest`` rejects empty id.
* ``EvolutionService.accept_individual`` — flips the individual's
  ``accepted`` flag in storage, patches the parent evolution
  (``status=ACCEPTED`` + ``accepted_individual_id`` + ``accepted_at`` +
  ``best_individual_id`` overridden), publishes an ``accepted`` SSE
  event, is idempotent on the same id, and refuses an attempt to switch
  individuals on an already-accepted evolution.
* Real-app smoke through master_api: auth, happy path, every error
  status (404 missing evolution, 404 missing individual, 409 switching
  after accept, 422 empty id).
"""

from __future__ import annotations

import importlib
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
bus_mod = importlib.import_module("src.services.evolution_event_bus")


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


def _evo_payload() -> Dict[str, Any]:
    return {
        "name": "evo-accept-test",
        "seed_chains": [{"chain_content": {"name": "seed", "template": "echo"}}],
        "fitness": {"prompt": "Is the answer correct?"},
        "objectives": ["accuracy"],
        "ga_config": {"population_size": 2, "max_iterations": 3},
    }


async def _build(monkeypatched_bus=None):
    storage = FakeStorage()
    bus = monkeypatched_bus or bus_mod.EvolutionEventBus()
    service = svc_mod.EvolutionService(storage=storage, event_bus=bus)
    evo = await service.create_evolution(models.EvolutionCreate.model_validate(_evo_payload()))
    return service, storage, bus, evo


async def _record(service, evo, **scores):
    return await service.record_individual(
        evo.evolution_id,
        models.IndividualCreate.model_validate(
            {"generation": 0, "chain_content": {"x": 1}, "fitness_scores": scores}
        ),
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_accept_request_rejects_empty_id():
    with pytest.raises(ValueError):
        models.AcceptIndividualRequest.model_validate({"individual_id": ""})


def test_accept_request_strips_optional_note_when_omitted():
    req = models.AcceptIndividualRequest.model_validate({"individual_id": "abc"})
    assert req.individual_id == "abc"
    assert req.note is None


# ---------------------------------------------------------------------------
# Service-level behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accept_flips_individual_flag_and_patches_evolution():
    service, storage, _, evo = await _build()
    ind = await _record(service, evo, accuracy=0.5)

    result = await service.accept_individual(evo.evolution_id, ind.individual_id, note="best so far")

    assert result.status == models.EvolutionStatus.ACCEPTED
    assert result.accepted_individual_id == ind.individual_id
    assert result.best_individual_id == ind.individual_id
    assert result.accepted_at is not None
    # Persisted shape matches the in-memory record.
    persisted_ind = await service.get_individual(evo.evolution_id, ind.individual_id)
    assert persisted_ind.accepted is True
    persisted_evo = await service.get_evolution(evo.evolution_id)
    assert persisted_evo.status == models.EvolutionStatus.ACCEPTED
    assert persisted_evo.accepted_individual_id == ind.individual_id
    # S3 metadata on the individual is bumped so cheap LIST filters see it.
    key = f"evolutions/{evo.evolution_id}/individuals/{ind.individual_id}.json"
    assert storage.metadata[key]["accepted"] == "true"


@pytest.mark.asyncio
async def test_accept_publishes_event_with_individual_payload():
    import asyncio

    service, _, bus, evo = await _build()
    ind = await _record(service, evo, accuracy=0.5)

    sub = bus.subscribe(evo.evolution_id)
    try:
        await service.accept_individual(evo.evolution_id, ind.individual_id)
        seen: List[str] = []
        while "accepted" not in seen:
            ev = await asyncio.wait_for(sub.__anext__(), timeout=1)
            seen.append(ev.event_type)
            if ev.event_type == "accepted":
                assert ev.payload["individual_id"] == ind.individual_id
                assert ev.payload["fitness_scores"] == {"accuracy": 0.5}
        assert "accepted" in seen
    finally:
        await sub.aclose()


@pytest.mark.asyncio
async def test_accept_overrides_metric_best():
    """Operator's pick wins even if a higher-scoring individual exists."""
    service, _, _, evo = await _build()
    weaker = await _record(service, evo, accuracy=0.3)
    stronger = await _record(service, evo, accuracy=0.9)
    # Sanity: the metric-based best is the higher-scoring one.
    assert (await service.get_evolution(evo.evolution_id)).best_individual_id == stronger.individual_id

    after = await service.accept_individual(evo.evolution_id, weaker.individual_id)
    assert after.best_individual_id == weaker.individual_id
    assert after.accepted_individual_id == weaker.individual_id


@pytest.mark.asyncio
async def test_accept_is_idempotent_on_same_id():
    service, _, _, evo = await _build()
    ind = await _record(service, evo, accuracy=0.5)
    first = await service.accept_individual(evo.evolution_id, ind.individual_id)
    second = await service.accept_individual(evo.evolution_id, ind.individual_id, note="reconfirm")
    # accepted_at should advance (it's set on every call) but the IDs stick.
    assert first.accepted_individual_id == second.accepted_individual_id
    assert first.status == second.status == models.EvolutionStatus.ACCEPTED


@pytest.mark.asyncio
async def test_accept_refuses_switching_after_accept():
    service, _, _, evo = await _build()
    a = await _record(service, evo, accuracy=0.5)
    b = await _record(service, evo, accuracy=0.6)
    await service.accept_individual(evo.evolution_id, a.individual_id)
    with pytest.raises(svc_mod.EvolutionAcceptError):
        await service.accept_individual(evo.evolution_id, b.individual_id)


@pytest.mark.asyncio
async def test_accept_unknown_evolution_raises_not_found():
    service, _, _, _ = await _build()
    with pytest.raises(svc_mod.EvolutionNotFoundError):
        await service.accept_individual("does-not-exist", "any")


@pytest.mark.asyncio
async def test_accept_unknown_individual_raises_not_found():
    service, _, _, evo = await _build()
    with pytest.raises(svc_mod.IndividualNotFoundError):
        await service.accept_individual(evo.evolution_id, "ghost-id")


# ---------------------------------------------------------------------------
# Real-app smoke
# ---------------------------------------------------------------------------


@pytest.fixture
def real_app(monkeypatch):
    monkeypatch.setenv("KAFKA__ENABLED", "false")
    monkeypatch.setenv("DATABASE__URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("MASTER_API_KEY", "accept-smoke-key")
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)

    for _stale in [m for m in list(sys.modules) if m == "src" or m.startswith("src.")]:
        sys.modules.pop(_stale, None)
    sys.path.insert(0, str(MASTER_SRC.parent))

    main_mod = importlib.import_module("src.main")
    evolutions_route = importlib.import_module("src.api.routes.evolutions")
    bus_mod_live = importlib.import_module("src.services.evolution_event_bus")
    bus_mod_live.reset_default_bus_for_tests()

    fake_storage = FakeStorage()

    class FakeServiceManager:
        def get_storage_service(self):
            return fake_storage

    evolutions_route.set_service_manager(FakeServiceManager())
    return main_mod.app, fake_storage


def _create_evo_and_individual(client, payload=None):
    body = payload or _evo_payload()
    create = client.post(
        "/api/v1/evolutions", headers={"X-API-Key": "accept-smoke-key"}, json=body
    )
    assert create.status_code == 201
    eid = create.json()["evolution_id"]
    rec = client.post(
        f"/api/v1/evolutions/{eid}/individuals",
        headers={"X-API-Key": "accept-smoke-key"},
        json={"generation": 0, "chain_content": {"x": 1}, "fitness_scores": {"accuracy": 0.5}},
    )
    assert rec.status_code == 201
    return eid, rec.json()["individual_id"]


def test_smoke_accept_happy_path(real_app):
    from fastapi.testclient import TestClient

    app, _ = real_app
    client = TestClient(app, raise_server_exceptions=False)
    eid, ind_id = _create_evo_and_individual(client)

    resp = client.post(
        f"/api/v1/evolutions/{eid}/accept",
        headers={"X-API-Key": "accept-smoke-key"},
        json={"individual_id": ind_id, "note": "demo"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["accepted_individual_id"] == ind_id
    assert body["best_individual_id"] == ind_id

    # Subsequent GET shows the new state.
    refreshed = client.get(
        f"/api/v1/evolutions/{eid}", headers={"X-API-Key": "accept-smoke-key"}
    ).json()
    assert refreshed["status"] == "accepted"
    # Individual's accepted flag now visible.
    ind = client.get(
        f"/api/v1/evolutions/{eid}/individuals/{ind_id}",
        headers={"X-API-Key": "accept-smoke-key"},
    ).json()
    assert ind["accepted"] is True


def test_smoke_accept_requires_auth(real_app):
    from fastapi.testclient import TestClient

    app, _ = real_app
    client = TestClient(app, raise_server_exceptions=False)
    eid, ind_id = _create_evo_and_individual(client)
    resp = client.post(
        f"/api/v1/evolutions/{eid}/accept", json={"individual_id": ind_id}
    )
    assert resp.status_code == 401


def test_smoke_accept_unknown_evolution_returns_404(real_app):
    from fastapi.testclient import TestClient

    app, _ = real_app
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/api/v1/evolutions/nope/accept",
        headers={"X-API-Key": "accept-smoke-key"},
        json={"individual_id": "anything"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "evolution_not_found"


def test_smoke_accept_unknown_individual_returns_404(real_app):
    from fastapi.testclient import TestClient

    app, _ = real_app
    client = TestClient(app, raise_server_exceptions=False)
    eid, _ = _create_evo_and_individual(client)
    resp = client.post(
        f"/api/v1/evolutions/{eid}/accept",
        headers={"X-API-Key": "accept-smoke-key"},
        json={"individual_id": "ghost"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "individual_not_found"


def test_smoke_accept_switch_after_accept_returns_409(real_app):
    from fastapi.testclient import TestClient

    app, _ = real_app
    client = TestClient(app, raise_server_exceptions=False)
    eid, ind1 = _create_evo_and_individual(client)
    # Record a second individual.
    rec2 = client.post(
        f"/api/v1/evolutions/{eid}/individuals",
        headers={"X-API-Key": "accept-smoke-key"},
        json={"generation": 0, "chain_content": {"x": 2}, "fitness_scores": {"accuracy": 0.7}},
    )
    ind2 = rec2.json()["individual_id"]
    # Accept first, then try to switch.
    a1 = client.post(
        f"/api/v1/evolutions/{eid}/accept",
        headers={"X-API-Key": "accept-smoke-key"},
        json={"individual_id": ind1},
    )
    assert a1.status_code == 200
    a2 = client.post(
        f"/api/v1/evolutions/{eid}/accept",
        headers={"X-API-Key": "accept-smoke-key"},
        json={"individual_id": ind2},
    )
    assert a2.status_code == 409
    assert a2.json()["detail"]["error"] == "already_accepted"


def test_smoke_accept_empty_id_returns_422(real_app):
    from fastapi.testclient import TestClient

    app, _ = real_app
    client = TestClient(app, raise_server_exceptions=False)
    eid, _ = _create_evo_and_individual(client)
    resp = client.post(
        f"/api/v1/evolutions/{eid}/accept",
        headers={"X-API-Key": "accept-smoke-key"},
        json={"individual_id": ""},
    )
    assert resp.status_code == 422
