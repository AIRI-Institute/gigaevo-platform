"""Tests for ``GET /api/v1/evolutions`` paginated list (CARE §4.9).

Covers:

* ``EvolutionService.list_evolutions`` — happy path returns records
  ordered most-recent-first; status / tag / q filters compose;
  cursor pagination walks forward; ``total_scanned`` reflects the
  post-filter set, not just the page.
* Skips per-evolution ``individuals/<id>.json`` blobs so they don't
  pollute the evolution-level list.
* Tolerates a corrupt blob without breaking the response.
* Real-app smoke through master_api — auth, happy list, status
  filter, cursor walk, paging-empty case.
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


def _payload(name: str, *, tags=("demo",)) -> Dict[str, Any]:
    return {
        "name": name,
        "seed_chains": [{"chain_content": {"name": "seed", "template": "echo"}}],
        "fitness": {"prompt": "Is the answer correct?"},
        "objectives": ["accuracy"],
        "ga_config": {"population_size": 2, "max_iterations": 3},
        "tags": list(tags),
    }


async def _build():
    storage = FakeStorage()
    bus = bus_mod.EvolutionEventBus()
    service = svc_mod.EvolutionService(storage=storage, event_bus=bus)
    return service, storage, bus


async def _create_many(service, names_and_tags):
    out = []
    for name, tags in names_and_tags:
        ec = models.EvolutionCreate.model_validate(_payload(name, tags=tags))
        out.append(await service.create_evolution(ec))
    return out


# ---------------------------------------------------------------------------
# Service-level behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_returns_most_recent_first():
    service, _, _ = await _build()
    records = await _create_many(service, [("alpha", ["demo"]), ("beta", ["demo"]), ("gamma", ["demo"])])
    page = await service.list_evolutions()
    # The most-recent-first ordering matches the inverse of the
    # creation order from the helper above.
    assert [r.name for r in page.items] == ["gamma", "beta", "alpha"]
    assert page.next_cursor is None
    assert page.total_scanned == 3
    # Ids round-trip.
    assert {r.evolution_id for r in page.items} == {r.evolution_id for r in records}


@pytest.mark.asyncio
async def test_list_status_filter():
    service, _, _ = await _build()
    a, b, c = await _create_many(
        service,
        [("alpha", ["demo"]), ("beta", ["demo"]), ("gamma", ["demo"])],
    )
    await service.update_status(b.evolution_id, models.EvolutionStatus.COMPLETED)

    completed_page = await service.list_evolutions(status=models.EvolutionStatus.COMPLETED)
    assert [r.name for r in completed_page.items] == ["beta"]

    queued_page = await service.list_evolutions(status=models.EvolutionStatus.QUEUED)
    assert sorted(r.name for r in queued_page.items) == ["alpha", "gamma"]


@pytest.mark.asyncio
async def test_list_tag_filter_case_insensitive():
    service, _, _ = await _build()
    await _create_many(
        service,
        [("alpha", ["Demo"]), ("beta", ["PROD"]), ("gamma", ["demo"])],
    )
    demo_page = await service.list_evolutions(tag="demo")
    assert sorted(r.name for r in demo_page.items) == ["alpha", "gamma"]
    prod_page = await service.list_evolutions(tag="prod")
    assert [r.name for r in prod_page.items] == ["beta"]


@pytest.mark.asyncio
async def test_list_q_substring_match():
    service, _, _ = await _build()
    await _create_many(
        service,
        [("weather-bot", ["x"]), ("finance-helper", ["x"]), ("weather-stable", ["x"])],
    )
    page = await service.list_evolutions(q="weather")
    assert sorted(r.name for r in page.items) == ["weather-bot", "weather-stable"]


@pytest.mark.asyncio
async def test_list_cursor_walks_forward():
    service, _, _ = await _build()
    records = await _create_many(
        service, [(f"e{i}", ["demo"]) for i in range(5)]
    )
    # Most-recent-first ordering: e4, e3, e2, e1, e0.
    p1 = await service.list_evolutions(limit=2)
    assert [r.name for r in p1.items] == ["e4", "e3"]
    assert p1.next_cursor == p1.items[-1].evolution_id

    p2 = await service.list_evolutions(limit=2, cursor=p1.next_cursor)
    assert [r.name for r in p2.items] == ["e2", "e1"]
    assert p2.next_cursor == p2.items[-1].evolution_id

    p3 = await service.list_evolutions(limit=2, cursor=p2.next_cursor)
    assert [r.name for r in p3.items] == ["e0"]
    assert p3.next_cursor is None  # final page

    # Sanity: the returned ids cover the entire creation set.
    walked = [r.evolution_id for r in p1.items + p2.items + p3.items]
    assert sorted(walked) == sorted(r.evolution_id for r in records)


@pytest.mark.asyncio
async def test_list_unknown_cursor_falls_back_to_first_page():
    service, _, _ = await _build()
    await _create_many(service, [(f"e{i}", ["x"]) for i in range(3)])
    page = await service.list_evolutions(cursor="bogus-id-no-such-evolution", limit=10)
    assert len(page.items) == 3


@pytest.mark.asyncio
async def test_list_skips_individuals_blobs():
    """Individuals live under ``evolutions/<eid>/individuals/<ind>.json``.

    They must not leak into the evolution-level list.
    """
    service, _, _ = await _build()
    evo = (await _create_many(service, [("alpha", ["demo"])]))[0]
    await service.record_individual(
        evo.evolution_id,
        models.IndividualCreate.model_validate(
            {"generation": 0, "chain_content": {}, "fitness_scores": {"accuracy": 0.5}}
        ),
    )
    page = await service.list_evolutions()
    assert len(page.items) == 1
    assert page.items[0].evolution_id == evo.evolution_id


@pytest.mark.asyncio
async def test_list_tolerates_corrupt_blob():
    service, storage, _ = await _build()
    await _create_many(service, [("alpha", ["x"])])
    storage.objects["evolutions/junk.json"] = b"not valid json{"
    page = await service.list_evolutions()
    assert len(page.items) == 1  # only the valid one


@pytest.mark.asyncio
async def test_list_combined_filters_compose():
    service, _, _ = await _build()
    a, b, c, d = await _create_many(
        service,
        [
            ("weather-bot", ["demo"]),
            ("weather-prod", ["prod"]),
            ("finance-bot", ["demo"]),
            ("weather-stale", ["demo"]),
        ],
    )
    await service.update_status(d.evolution_id, models.EvolutionStatus.COMPLETED)

    page = await service.list_evolutions(
        status=models.EvolutionStatus.QUEUED, tag="demo", q="weather"
    )
    # Only "weather-bot" matches all three filters.
    assert [r.name for r in page.items] == ["weather-bot"]


# ---------------------------------------------------------------------------
# Real-app smoke
# ---------------------------------------------------------------------------


@pytest.fixture
def real_app(monkeypatch):
    monkeypatch.setenv("KAFKA__ENABLED", "false")
    monkeypatch.setenv("DATABASE__URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("MASTER_API_KEY", "list-smoke-key")
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


def test_smoke_list_requires_auth(real_app):
    from fastapi.testclient import TestClient

    app, _ = real_app
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/v1/evolutions")
    assert resp.status_code == 401


def test_smoke_empty_list_returns_zero_items(real_app):
    from fastapi.testclient import TestClient

    app, _ = real_app
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/v1/evolutions", headers={"X-API-Key": "list-smoke-key"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["next_cursor"] is None
    assert body["total_scanned"] == 0


def test_smoke_list_after_three_creates(real_app):
    from fastapi.testclient import TestClient

    app, _ = real_app
    client = TestClient(app, raise_server_exceptions=False)
    for name in ["alpha", "beta", "gamma"]:
        r = client.post(
            "/api/v1/evolutions",
            headers={"X-API-Key": "list-smoke-key"},
            json=_payload(name),
        )
        assert r.status_code == 201

    resp = client.get(
        "/api/v1/evolutions?limit=2",
        headers={"X-API-Key": "list-smoke-key"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [i["name"] for i in body["items"]] == ["gamma", "beta"]
    assert body["next_cursor"]  # there's one more record

    # Follow the cursor.
    p2 = client.get(
        f"/api/v1/evolutions?limit=2&cursor={body['next_cursor']}",
        headers={"X-API-Key": "list-smoke-key"},
    ).json()
    assert [i["name"] for i in p2["items"]] == ["alpha"]
    assert p2["next_cursor"] is None


def test_smoke_list_status_filter(real_app):
    from fastapi.testclient import TestClient

    app, _ = real_app
    client = TestClient(app, raise_server_exceptions=False)
    # Create 2 evolutions; we can't easily mutate status via HTTP yet —
    # use the §4.4 accept path on one of them.
    eids = []
    for name in ["alpha", "beta"]:
        r = client.post(
            "/api/v1/evolutions",
            headers={"X-API-Key": "list-smoke-key"},
            json=_payload(name),
        )
        eids.append(r.json()["evolution_id"])
    # Promote one via accept: needs an individual first.
    ind_r = client.post(
        f"/api/v1/evolutions/{eids[1]}/individuals",
        headers={"X-API-Key": "list-smoke-key"},
        json={"generation": 0, "chain_content": {}, "fitness_scores": {"accuracy": 0.5}},
    )
    assert ind_r.status_code == 201
    ind_id = ind_r.json()["individual_id"]
    accept = client.post(
        f"/api/v1/evolutions/{eids[1]}/accept",
        headers={"X-API-Key": "list-smoke-key"},
        json={"individual_id": ind_id},
    )
    assert accept.status_code == 200

    accepted = client.get(
        "/api/v1/evolutions?status=accepted",
        headers={"X-API-Key": "list-smoke-key"},
    ).json()
    assert [i["name"] for i in accepted["items"]] == ["beta"]
    queued = client.get(
        "/api/v1/evolutions?status=queued",
        headers={"X-API-Key": "list-smoke-key"},
    ).json()
    assert [i["name"] for i in queued["items"]] == ["alpha"]


def test_smoke_list_q_substring(real_app):
    from fastapi.testclient import TestClient

    app, _ = real_app
    client = TestClient(app, raise_server_exceptions=False)
    for name in ["weather-bot", "finance-helper", "weather-stable"]:
        client.post(
            "/api/v1/evolutions",
            headers={"X-API-Key": "list-smoke-key"},
            json=_payload(name),
        )
    page = client.get(
        "/api/v1/evolutions?q=weather",
        headers={"X-API-Key": "list-smoke-key"},
    ).json()
    assert sorted(i["name"] for i in page["items"]) == ["weather-bot", "weather-stable"]


def test_smoke_list_rejects_out_of_range_limit(real_app):
    from fastapi.testclient import TestClient

    app, _ = real_app
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get(
        "/api/v1/evolutions?limit=0",
        headers={"X-API-Key": "list-smoke-key"},
    )
    assert resp.status_code == 422
    resp = client.get(
        "/api/v1/evolutions?limit=9999",
        headers={"X-API-Key": "list-smoke-key"},
    )
    assert resp.status_code == 422
