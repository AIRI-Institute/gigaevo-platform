"""Tests for evolution lifecycle control endpoints."""

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

    async def list_objects(self, prefix: str = "", recursive: bool = False) -> List[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))


def _payload(**overrides: Any) -> Dict[str, Any]:
    base = {
        "name": "evo-control-test",
        "seed_chains": [{"chain_content": {"name": "seed"}}],
        "fitness": {"prompt": "score it"},
        "objectives": ["accuracy"],
        "ga_config": {"population_size": 2, "max_iterations": 3},
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_pause_resume_cancel_state_transitions():
    service = svc_mod.EvolutionService(
        storage=FakeStorage(), event_bus=bus_mod.EvolutionEventBus()
    )
    evo = await service.create_evolution(models.EvolutionCreate.model_validate(_payload()))

    paused = await service.pause_evolution(evo.evolution_id)
    assert paused.status == models.EvolutionStatus.PAUSED

    resumed = await service.resume_evolution(evo.evolution_id)
    assert resumed.status == models.EvolutionStatus.RUNNING

    cancelled = await service.cancel_evolution(evo.evolution_id)
    assert cancelled.status == models.EvolutionStatus.CANCELLED


@pytest.mark.asyncio
async def test_resume_requires_paused_state():
    service = svc_mod.EvolutionService(
        storage=FakeStorage(), event_bus=bus_mod.EvolutionEventBus()
    )
    evo = await service.create_evolution(models.EvolutionCreate.model_validate(_payload()))
    with pytest.raises(svc_mod.EvolutionControlError):
        await service.resume_evolution(evo.evolution_id)


@pytest.fixture
def real_app(monkeypatch):
    monkeypatch.setenv("KAFKA__ENABLED", "false")
    monkeypatch.setenv("DATABASE__URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("MASTER_API_KEY", "control-key")
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
    return main_mod.app


def test_smoke_control_endpoints(real_app):
    from fastapi.testclient import TestClient

    client = TestClient(real_app, raise_server_exceptions=False)
    create = client.post(
        "/api/v1/evolutions",
        headers={"X-API-Key": "control-key"},
        json=_payload(),
    )
    assert create.status_code == 201, create.text
    eid = create.json()["evolution_id"]

    pause = client.post(f"/api/v1/evolutions/{eid}/pause", headers={"X-API-Key": "control-key"})
    assert pause.status_code == 200, pause.text
    assert pause.json()["status"] == "paused"

    resume = client.post(f"/api/v1/evolutions/{eid}/resume", headers={"X-API-Key": "control-key"})
    assert resume.status_code == 200, resume.text
    assert resume.json()["status"] == "running"

    cancel = client.post(f"/api/v1/evolutions/{eid}/cancel", headers={"X-API-Key": "control-key"})
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["status"] == "cancelled"

    resume_again = client.post(f"/api/v1/evolutions/{eid}/resume", headers={"X-API-Key": "control-key"})
    assert resume_again.status_code == 409
    assert resume_again.json()["detail"]["error"] == "invalid_transition"
