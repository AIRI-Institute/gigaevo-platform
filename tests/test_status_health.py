"""Tests for Platform health fields consumed by CARE onboarding."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MASTER_SRC = REPO_ROOT / "master_api" / "src"

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(MASTER_SRC.parent))

for _stale in [m for m in list(sys.modules) if m == "src" or m.startswith("src.")]:
    sys.modules.pop(_stale, None)

instance_models = importlib.import_module("src.models.instance")
status_mod = importlib.import_module("src.services.status_service")


class FakeDb:
    async def list_experiments(self):
        return []


class FakeInstanceService:
    async def list_instances(self):
        return [
            instance_models.RunnerInstance(
                id="r1",
                name="runner 1",
                endpoint_url="http://runner-1:8001",
                status=instance_models.RunnerInstanceStatus.READY,
            ),
            instance_models.RunnerInstance(
                id="r2",
                name="runner 2",
                endpoint_url="http://runner-2:8001",
                status=instance_models.RunnerInstanceStatus.BUSY,
            ),
            instance_models.RunnerInstance(
                id="r3",
                name="runner 3",
                endpoint_url="http://runner-3:8001",
                status=instance_models.RunnerInstanceStatus.ERROR,
            ),
        ]


class FakeServiceManager:
    db_service = FakeDb()
    storage_service = object()
    kafka_service = None
    instance_service = FakeInstanceService()

    def is_healthy(self):
        return True


@pytest.mark.asyncio
async def test_system_health_exposes_auth_and_runner_pool(monkeypatch):
    monkeypatch.setenv("MASTER_API_KEY", "platform-key")
    service = status_mod.StatusService(FakeServiceManager())

    health = await service.get_system_health()

    assert health["auth"] == "required"
    assert health["runner_pool"] == {
        "total": 3,
        "ready_count": 1,
        "busy_count": 1,
        "initializing_count": 0,
        "error_count": 1,
        "offline_count": 0,
    }
    assert health["components"]["runners"] == "initializing"


@pytest.mark.asyncio
async def test_system_health_reports_open_auth_when_no_key(monkeypatch):
    monkeypatch.delenv("MASTER_API_KEY", raising=False)
    service = status_mod.StatusService(FakeServiceManager())

    health = await service.get_system_health()

    assert health["auth"] == "open"
