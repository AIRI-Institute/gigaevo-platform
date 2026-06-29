"""Tests for /individuals (CARE §4.2).

Coverage:

* ``IndividualCreate`` schema.
* ``EvolutionService.record_individual`` — persists JSON, updates
  ``current_generation`` and ``best_individual_id`` correctly, publishes
  ``individual_evaluated`` and (when applicable) ``best_updated`` events,
  and refuses unknown objective keys.
* Pareto-front computation — single + multi-objective, with mixed
  ``higher_is_better`` semantics from the parent evolution.
* ``EvolutionService.list_individuals`` — generation filter, pareto
  filter, limit, deterministic ordering.
* Real-app smoke through master_api: POST /individuals (201, 404, 422),
  GET /individuals list with filters, GET /individuals/{id} (200, 404),
  auth enforcement.
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
        # Mirror MinIO's prefix-list semantics; recursive=True returns all
        # matching keys regardless of depth.
        return sorted(k for k in self.objects if k.startswith(prefix))


def _evo_payload(objectives=("accuracy",), higher_is_better=True) -> Dict[str, Any]:
    return {
        "name": "evo-individuals-test",
        "seed_chains": [{"chain_content": {"name": "seed", "template": "echo"}}],
        "fitness": {"prompt": "Is the answer correct?", "higher_is_better": higher_is_better},
        "objectives": list(objectives),
        "ga_config": {"population_size": 2, "max_iterations": 5},
    }


async def _new_evolution(service, **payload_overrides) -> models.EvolutionResponse:
    payload = models.EvolutionCreate.model_validate(_evo_payload(**payload_overrides))
    return await service.create_evolution(payload)


def _ind(generation=0, **scores) -> Dict[str, Any]:
    return {
        "generation": generation,
        "chain_content": {"text": f"gen{generation}"},
        "fitness_scores": scores,
    }


# ---------------------------------------------------------------------------
# record_individual
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_individual_persists_and_returns_uuid():
    storage = FakeStorage()
    bus = bus_mod.EvolutionEventBus()
    service = svc_mod.EvolutionService(storage=storage, event_bus=bus)
    evo = await _new_evolution(service)

    payload = models.IndividualCreate.model_validate(_ind(accuracy=0.7))
    ind = await service.record_individual(evo.evolution_id, payload)
    assert ind.evolution_id == evo.evolution_id
    assert ind.individual_id  # uuid
    assert ind.fitness_scores == {"accuracy": 0.7}

    key = f"evolutions/{evo.evolution_id}/individuals/{ind.individual_id}.json"
    assert key in storage.objects
    assert storage.metadata[key]["type"] == "evolution_individual"
    assert storage.metadata[key]["generation"] == "0"


@pytest.mark.asyncio
async def test_record_individual_rejects_unknown_objective():
    storage = FakeStorage()
    bus = bus_mod.EvolutionEventBus()
    service = svc_mod.EvolutionService(storage=storage, event_bus=bus)
    evo = await _new_evolution(service, objectives=("accuracy",))

    bad = models.IndividualCreate.model_validate(_ind(accuracy=0.5, latency_ms=100))
    with pytest.raises(svc_mod.IndividualValidationError):
        await service.record_individual(evo.evolution_id, bad)


@pytest.mark.asyncio
async def test_record_individual_bumps_current_generation_only_when_higher():
    storage = FakeStorage()
    bus = bus_mod.EvolutionEventBus()
    service = svc_mod.EvolutionService(storage=storage, event_bus=bus)
    evo = await _new_evolution(service)

    await service.record_individual(evo.evolution_id, models.IndividualCreate.model_validate(_ind(0, accuracy=0.1)))
    after_0 = await service.get_evolution(evo.evolution_id)
    assert after_0.current_generation == 0

    await service.record_individual(evo.evolution_id, models.IndividualCreate.model_validate(_ind(2, accuracy=0.2)))
    after_2 = await service.get_evolution(evo.evolution_id)
    assert after_2.current_generation == 2

    # Earlier generation must NOT roll back current_generation.
    await service.record_individual(evo.evolution_id, models.IndividualCreate.model_validate(_ind(1, accuracy=0.3)))
    after_1 = await service.get_evolution(evo.evolution_id)
    assert after_1.current_generation == 2


@pytest.mark.asyncio
async def test_record_individual_updates_best_when_score_wins_higher_is_better():
    storage = FakeStorage()
    bus = bus_mod.EvolutionEventBus()
    service = svc_mod.EvolutionService(storage=storage, event_bus=bus)
    evo = await _new_evolution(service)

    first = await service.record_individual(evo.evolution_id, models.IndividualCreate.model_validate(_ind(accuracy=0.3)))
    after = await service.get_evolution(evo.evolution_id)
    assert after.best_individual_id == first.individual_id

    # Worse score → best UNCHANGED.
    await service.record_individual(evo.evolution_id, models.IndividualCreate.model_validate(_ind(accuracy=0.2)))
    after = await service.get_evolution(evo.evolution_id)
    assert after.best_individual_id == first.individual_id

    # Better score → best flips.
    third = await service.record_individual(evo.evolution_id, models.IndividualCreate.model_validate(_ind(accuracy=0.9)))
    after = await service.get_evolution(evo.evolution_id)
    assert after.best_individual_id == third.individual_id


@pytest.mark.asyncio
async def test_record_individual_lower_is_better_inverts_direction():
    storage = FakeStorage()
    bus = bus_mod.EvolutionEventBus()
    service = svc_mod.EvolutionService(storage=storage, event_bus=bus)
    evo = await _new_evolution(service, objectives=("latency_ms",), higher_is_better=False)

    a = await service.record_individual(
        evo.evolution_id, models.IndividualCreate.model_validate(_ind(latency_ms=500))
    )
    assert (await service.get_evolution(evo.evolution_id)).best_individual_id == a.individual_id

    b = await service.record_individual(
        evo.evolution_id, models.IndividualCreate.model_validate(_ind(latency_ms=100))
    )
    assert (await service.get_evolution(evo.evolution_id)).best_individual_id == b.individual_id

    # 800 ms loses against 100.
    await service.record_individual(
        evo.evolution_id, models.IndividualCreate.model_validate(_ind(latency_ms=800))
    )
    assert (await service.get_evolution(evo.evolution_id)).best_individual_id == b.individual_id


@pytest.mark.asyncio
async def test_record_individual_publishes_individual_evaluated_and_best_updated():
    storage = FakeStorage()
    bus = bus_mod.EvolutionEventBus()
    service = svc_mod.EvolutionService(storage=storage, event_bus=bus)
    evo = await _new_evolution(service)

    sub = bus.subscribe(evo.evolution_id)
    try:
        await service.record_individual(
            evo.evolution_id, models.IndividualCreate.model_validate(_ind(accuracy=0.5))
        )
        # First-ever individual wins by default → expect individual_evaluated
        # then best_updated (status_changed in between, fired by
        # update_status, is fine — we just need to see both target events).
        seen: List[str] = []
        import asyncio

        while "best_updated" not in seen:
            ev = await asyncio.wait_for(sub.__anext__(), timeout=1)
            seen.append(ev.event_type)
        assert "individual_evaluated" in seen
        assert "best_updated" in seen
    finally:
        await sub.aclose()


# ---------------------------------------------------------------------------
# pareto_front
# ---------------------------------------------------------------------------


def _make_ind(eid: str, **scores) -> models.IndividualResponse:
    return models.IndividualResponse(
        evolution_id=eid,
        generation=0,
        chain_content={"x": 1},
        fitness_scores=scores,
    )


def test_pareto_higher_is_better_single_objective():
    items = [_make_ind("e", accuracy=v) for v in [0.1, 0.5, 0.9, 0.3]]
    front = svc_mod.pareto_front(items, ["accuracy"], higher_is_better=True)
    assert len(front) == 1
    assert front[0].fitness_scores["accuracy"] == 0.9


def test_pareto_higher_is_better_multi_objective():
    # Construct three points on (accuracy, throughput); want both maximised.
    # (.9, .1), (.5, .5), (.1, .9) — all on the front; (.3, .3) dominated.
    objectives = ["accuracy", "throughput"]
    items = [
        _make_ind("e", accuracy=0.9, throughput=0.1),
        _make_ind("e", accuracy=0.5, throughput=0.5),
        _make_ind("e", accuracy=0.1, throughput=0.9),
        _make_ind("e", accuracy=0.3, throughput=0.3),  # dominated by middle
    ]
    front = svc_mod.pareto_front(items, objectives, higher_is_better=True)
    scores = sorted((i.fitness_scores["accuracy"], i.fitness_scores["throughput"]) for i in front)
    assert scores == [(0.1, 0.9), (0.5, 0.5), (0.9, 0.1)]


def test_pareto_lower_is_better_excludes_high_loss():
    items = [_make_ind("e", loss=v) for v in [0.1, 0.5, 0.9]]
    front = svc_mod.pareto_front(items, ["loss"], higher_is_better=False)
    assert len(front) == 1
    assert front[0].fitness_scores["loss"] == 0.1


def test_pareto_missing_score_treated_as_worst():
    items = [
        _make_ind("e", accuracy=0.5),
        _make_ind("e"),  # missing → -inf under higher_is_better
    ]
    front = svc_mod.pareto_front(items, ["accuracy"], higher_is_better=True)
    assert len(front) == 1
    assert front[0].fitness_scores == {"accuracy": 0.5}


# ---------------------------------------------------------------------------
# list_individuals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_individuals_filters_and_paretoes():
    storage = FakeStorage()
    bus = bus_mod.EvolutionEventBus()
    service = svc_mod.EvolutionService(storage=storage, event_bus=bus)
    evo = await _new_evolution(service, objectives=("acc", "tpt"))

    async def rec(gen, acc, tpt):
        return await service.record_individual(
            evo.evolution_id, models.IndividualCreate.model_validate({
                "generation": gen,
                "chain_content": {"g": gen, "a": acc, "t": tpt},
                "fitness_scores": {"acc": acc, "tpt": tpt},
            })
        )

    await rec(0, 0.1, 0.9)
    await rec(0, 0.9, 0.1)
    await rec(0, 0.3, 0.3)  # dominated by both — out of Pareto
    await rec(1, 0.5, 0.5)

    everything = await service.list_individuals(evo.evolution_id)
    assert len(everything) == 4
    # Ordering: by generation then created_at (gen 0 trio first, gen 1 last).
    assert [i.generation for i in everything] == [0, 0, 0, 1]

    only_gen_0 = await service.list_individuals(evo.evolution_id, generation=0)
    assert len(only_gen_0) == 3
    assert all(i.generation == 0 for i in only_gen_0)

    front = await service.list_individuals(evo.evolution_id, pareto=True)
    # All four are on the front (multi-objective): the (0.3, 0.3) is
    # dominated by (0.5, 0.5), so we should see 3 items.
    scores = sorted((i.fitness_scores["acc"], i.fitness_scores["tpt"]) for i in front)
    assert (0.3, 0.3) not in scores
    assert len(front) == 3

    capped = await service.list_individuals(evo.evolution_id, limit=2)
    assert len(capped) == 2


@pytest.mark.asyncio
async def test_list_individuals_skips_corrupt_blob():
    storage = FakeStorage()
    bus = bus_mod.EvolutionEventBus()
    service = svc_mod.EvolutionService(storage=storage, event_bus=bus)
    evo = await _new_evolution(service)

    await service.record_individual(
        evo.evolution_id, models.IndividualCreate.model_validate(_ind(accuracy=0.5))
    )
    # Drop a junk file under the same prefix.
    storage.objects[f"evolutions/{evo.evolution_id}/individuals/junk.json"] = b"not valid json{"
    result = await service.list_individuals(evo.evolution_id)
    # Only the valid one survives.
    assert len(result) == 1


@pytest.mark.asyncio
async def test_list_individuals_unknown_evolution_raises():
    storage = FakeStorage()
    bus = bus_mod.EvolutionEventBus()
    service = svc_mod.EvolutionService(storage=storage, event_bus=bus)
    with pytest.raises(svc_mod.EvolutionNotFoundError):
        await service.list_individuals("nope")


# ---------------------------------------------------------------------------
# Real-app smoke through master_api
# ---------------------------------------------------------------------------


@pytest.fixture
def real_app(monkeypatch):
    monkeypatch.setenv("KAFKA__ENABLED", "false")
    monkeypatch.setenv("DATABASE__URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("MASTER_API_KEY", "ind-smoke-key")
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


def test_smoke_record_and_list_individuals(real_app):
    from fastapi.testclient import TestClient

    app, _ = real_app
    client = TestClient(app, raise_server_exceptions=False)

    create = client.post(
        "/api/v1/evolutions",
        headers={"X-API-Key": "ind-smoke-key"},
        json=_evo_payload(objectives=("acc", "tpt")),
    )
    assert create.status_code == 201
    eid = create.json()["evolution_id"]

    # Auth still enforced.
    no_auth = client.post(f"/api/v1/evolutions/{eid}/individuals", json=_ind(acc=0.5, tpt=0.5))
    assert no_auth.status_code == 401

    rec1 = client.post(
        f"/api/v1/evolutions/{eid}/individuals",
        headers={"X-API-Key": "ind-smoke-key"},
        json=_ind(generation=0, acc=0.9, tpt=0.1),
    )
    assert rec1.status_code == 201, rec1.text
    ind1_id = rec1.json()["individual_id"]

    client.post(
        f"/api/v1/evolutions/{eid}/individuals",
        headers={"X-API-Key": "ind-smoke-key"},
        json=_ind(generation=0, acc=0.5, tpt=0.5),
    )
    client.post(
        f"/api/v1/evolutions/{eid}/individuals",
        headers={"X-API-Key": "ind-smoke-key"},
        json=_ind(generation=1, acc=0.95, tpt=0.05),
    )

    # List everything.
    listing = client.get(
        f"/api/v1/evolutions/{eid}/individuals",
        headers={"X-API-Key": "ind-smoke-key"},
    )
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["total"] == 3
    assert body["primary_objective"] == "acc"
    assert body["higher_is_better"] is True

    # Generation filter.
    only_1 = client.get(
        f"/api/v1/evolutions/{eid}/individuals?generation=1",
        headers={"X-API-Key": "ind-smoke-key"},
    )
    assert only_1.status_code == 200
    assert only_1.json()["total"] == 1

    # Pareto filter.
    front = client.get(
        f"/api/v1/evolutions/{eid}/individuals?pareto=true",
        headers={"X-API-Key": "ind-smoke-key"},
    )
    assert front.status_code == 200
    assert front.json()["pareto_front"] is True
    # All three are non-dominated under (max acc, max tpt) — none dominates
    # another because tradeoffs are strict.
    assert front.json()["total"] == 3

    # Single-individual get.
    one = client.get(
        f"/api/v1/evolutions/{eid}/individuals/{ind1_id}",
        headers={"X-API-Key": "ind-smoke-key"},
    )
    assert one.status_code == 200
    assert one.json()["individual_id"] == ind1_id

    # 404 on unknown individual.
    missing = client.get(
        f"/api/v1/evolutions/{eid}/individuals/does-not-exist",
        headers={"X-API-Key": "ind-smoke-key"},
    )
    assert missing.status_code == 404

    # 404 on unknown evolution.
    unknown_evo = client.post(
        "/api/v1/evolutions/nope/individuals",
        headers={"X-API-Key": "ind-smoke-key"},
        json=_ind(acc=0.1, tpt=0.1),
    )
    assert unknown_evo.status_code == 404

    # 422 on unknown objective key.
    bad_obj = client.post(
        f"/api/v1/evolutions/{eid}/individuals",
        headers={"X-API-Key": "ind-smoke-key"},
        json=_ind(acc=0.1, tpt=0.1, unknown_axis=0.5),
    )
    assert bad_obj.status_code == 422
