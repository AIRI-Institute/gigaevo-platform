import json
import os
import sys
from types import SimpleNamespace

import pytest

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
MASTER_API_DIR = os.path.join(ROOT_DIR, "master_api")
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
if MASTER_API_DIR not in sys.path:
    sys.path.insert(0, MASTER_API_DIR)

from master_api.src.api.routes import experiments as experiment_routes
from master_api.src.api.routes import results as result_routes
from master_api.src.services.runner_instance_service import RunnerInstanceService
from master_api.src.models.experiment import (
    ChainExperimentCreate,
    ChainValidationCriteria,
    Experiment,
    PromptExperimentCreate,
    PromptValidationCriteria,
)


class _FakeDbService:
    def __init__(self, experiment=None):
        self.experiment = experiment
        self.created = None

    async def create_experiment(self, experiment_create, experiment_id):
        self.created = (experiment_create, experiment_id)

    async def get_experiment(self, experiment_id):
        return self.experiment


class _FakeExperimentService:
    def __init__(self, db_service: _FakeDbService):
        self.db_service = db_service

    async def get_experiment(self, experiment_id: str):
        if not self.db_service.created:
            return None
        experiment_create, _ = self.db_service.created
        return Experiment(
            id=experiment_id,
            name=experiment_create.name,
            config=experiment_create.config,
            data_path=experiment_create.data_path,
        )


class _FakeExperimentServiceManager:
    def __init__(self, db_service: _FakeDbService):
        self._db_service = db_service

    def get_db_service(self):
        return self._db_service


class _FakeRunnerInstanceService:
    def __init__(self, *, allocated_instance=None, runner_status="READY"):
        self.allocated_instance = allocated_instance
        self.runner_status = runner_status
        self.allocate_calls = []
        self.release_calls = []

    async def allocate_specific_runner(self, runner_id: str, experiment_id: str):
        self.allocate_calls.append((runner_id, experiment_id))
        return self.allocated_instance

    async def get_instance(self, runner_id: str):
        return SimpleNamespace(status=self.runner_status)

    async def release_runner_by_id_if_experiment(self, runner_id: str, experiment_id: str):
        self.release_calls.append((runner_id, experiment_id))
        return True


class _FakeDockerService:
    def __init__(self):
        self.initialize_calls = []

    async def initialize_container(self, image_name, network_name, environment_vars=None, volumes=None):
        self.initialize_calls.append(
            {
                "image_name": image_name,
                "network_name": network_name,
                "environment_vars": dict(environment_vars or {}),
                "volumes": dict(volumes or {}),
            }
        )
        return True

    async def get_health_status(self):
        return True, True


class _FakeResultServiceManager:
    def __init__(self, db_service: _FakeDbService, instance_service: _FakeRunnerInstanceService):
        self._db_service = db_service
        self.instance_service = instance_service

    def get_db_service(self):
        return self._db_service


class _FakeHttpxResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeHttpxClient:
    def __init__(self, responses, calls, *, timeout=None):
        self._responses = responses
        self._calls = calls
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json):
        self._calls.append((url, json, self.timeout))
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_prompt_route_persists_memory_namespace(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(experiment_routes, "is_allowed_llm_model", lambda model_id: True)
    db_service = _FakeDbService()
    service_manager = _FakeExperimentServiceManager(db_service)
    service = _FakeExperimentService(db_service)
    previous = experiment_routes._service_manager
    experiment_routes._service_manager = service_manager

    try:
        prompt = PromptExperimentCreate(
            name="Prompt Experiment",
            description="desc",
            data_path="data/train.csv",
            target_column="target",
            base_prompt="Answer {question}",
            validation_criteria=PromptValidationCriteria(
                validation_type="Binary (0/1)",
                binary_method="equality",
            ),
            enable_memory=True,
            memory_namespace="shared-bank",
        )

        await experiment_routes.create_prompt_experiment(prompt, service=service)

        created_experiment, _ = db_service.created
        assert created_experiment.config.parameters["enable_memory"] is True
        assert created_experiment.config.parameters["memory_namespace"] == "shared-bank"
    finally:
        experiment_routes._service_manager = previous


@pytest.mark.asyncio
async def test_chain_route_persists_memory_namespace(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(experiment_routes, "is_allowed_llm_model", lambda model_id: True)
    db_service = _FakeDbService()
    service_manager = _FakeExperimentServiceManager(db_service)
    service = _FakeExperimentService(db_service)
    previous = experiment_routes._service_manager
    experiment_routes._service_manager = service_manager

    try:
        chain = ChainExperimentCreate(
            name="Chain Experiment",
            description="desc",
            data_path="data/train.csv",
            target_column="target",
            base_chain_config=json.dumps({"steps": [{"number": 1, "title": "Step 1"}]}),
            validation_criteria=ChainValidationCriteria(),
            enable_memory=True,
            memory_namespace="shared-bank",
        )

        await experiment_routes.create_chain_experiment(chain, service=service)

        created_experiment, _ = db_service.created
        assert created_experiment.config.parameters["enable_memory"] is True
        assert created_experiment.config.parameters["memory_namespace"] == "shared-bank"
    finally:
        experiment_routes._service_manager = previous


@pytest.mark.asyncio
async def test_upload_to_memory_uses_assigned_runner_and_stored_namespace(monkeypatch: pytest.MonkeyPatch):
    db_service = _FakeDbService(
        experiment=SimpleNamespace(
            status="completed",
            config={
                "assigned_runner_id": "runner-2",
                "parameters": {"memory_namespace": "shared-bank"},
            },
        )
    )
    instance_service = _FakeRunnerInstanceService(
        allocated_instance=SimpleNamespace(endpoint_url="http://runner-2:8001", status="READY")
    )
    service_manager = _FakeResultServiceManager(db_service, instance_service)
    previous = result_routes._service_manager
    result_routes._service_manager = service_manager

    calls = []
    responses = [_FakeHttpxResponse(200, {"success": True})]
    monkeypatch.setattr(
        result_routes.httpx,
        "AsyncClient",
        lambda timeout=None: _FakeHttpxClient(responses, calls, timeout=timeout),
    )

    try:
        response = await result_routes.upload_to_memory("exp_demo")
        payload = json.loads(response.body)

        assert response.status_code == 200
        assert payload["success"] is True
        assert payload["namespace"] == "shared-bank"
        assert instance_service.allocate_calls == [("runner-2", "exp_demo")]
        assert instance_service.release_calls == [("runner-2", "exp_demo")]
        assert calls == [
            (
                "http://runner-2:8001/api/v1/experiments/exp_demo/upload-to-memory",
                {"memory_namespace": "shared-bank"},
                660.0,
            )
        ]
    finally:
        result_routes._service_manager = previous


@pytest.mark.asyncio
async def test_upload_to_memory_allows_cancelled_experiments(monkeypatch: pytest.MonkeyPatch):
    db_service = _FakeDbService(
        experiment=SimpleNamespace(
            status="cancelled",
            config={
                "assigned_runner_id": "runner-4",
                "parameters": {"memory_namespace": "shared-bank"},
            },
        )
    )
    instance_service = _FakeRunnerInstanceService(
        allocated_instance=SimpleNamespace(endpoint_url="http://runner-4:8001", status="READY")
    )
    service_manager = _FakeResultServiceManager(db_service, instance_service)
    previous = result_routes._service_manager
    result_routes._service_manager = service_manager

    calls = []
    responses = [_FakeHttpxResponse(200, {"success": True})]
    monkeypatch.setattr(
        result_routes.httpx,
        "AsyncClient",
        lambda timeout=None: _FakeHttpxClient(responses, calls, timeout=timeout),
    )

    try:
        response = await result_routes.upload_to_memory("exp_demo")
        payload = json.loads(response.body)

        assert response.status_code == 200
        assert payload["success"] is True
        assert payload["namespace"] == "shared-bank"
        assert instance_service.allocate_calls == [("runner-4", "exp_demo")]
        assert instance_service.release_calls == [("runner-4", "exp_demo")]
        assert calls == [
            (
                "http://runner-4:8001/api/v1/experiments/exp_demo/upload-to-memory",
                {"memory_namespace": "shared-bank"},
                660.0,
            )
        ]
    finally:
        result_routes._service_manager = previous


@pytest.mark.asyncio
async def test_upload_to_memory_falls_back_to_experiment_id_namespace(monkeypatch: pytest.MonkeyPatch):
    db_service = _FakeDbService(
        experiment=SimpleNamespace(
            status="completed",
            config={"assigned_runner_id": "runner-3", "parameters": {}},
        )
    )
    instance_service = _FakeRunnerInstanceService(
        allocated_instance=SimpleNamespace(endpoint_url="http://runner-3:8001", status="READY")
    )
    service_manager = _FakeResultServiceManager(db_service, instance_service)
    previous = result_routes._service_manager
    result_routes._service_manager = service_manager

    calls = []
    responses = [_FakeHttpxResponse(200, {"success": True})]
    monkeypatch.setattr(
        result_routes.httpx,
        "AsyncClient",
        lambda timeout=None: _FakeHttpxClient(responses, calls, timeout=timeout),
    )

    try:
        response = await result_routes.upload_to_memory("exp_demo")
        payload = json.loads(response.body)

        assert response.status_code == 200
        assert payload["namespace"] == "exp_demo"
        assert calls[0][1] == {"memory_namespace": "exp_demo"}
    finally:
        result_routes._service_manager = previous


@pytest.mark.asyncio
async def test_upload_to_memory_rejects_non_terminal_status():
    db_service = _FakeDbService(
        experiment=SimpleNamespace(
            status="running",
            config={"assigned_runner_id": "runner-9", "parameters": {}},
        )
    )
    instance_service = _FakeRunnerInstanceService()
    service_manager = _FakeResultServiceManager(db_service, instance_service)
    previous = result_routes._service_manager
    result_routes._service_manager = service_manager

    try:
        response = await result_routes.upload_to_memory("exp_demo")
        payload = json.loads(response.body)

        assert response.status_code == 400
        assert payload["error"] == "Experiment must be completed, terminated, or cancelled (current: running)"
        assert instance_service.allocate_calls == []
        assert instance_service.release_calls == []
    finally:
        result_routes._service_manager = previous


@pytest.mark.asyncio
async def test_upload_to_memory_returns_retryable_error_when_original_runner_is_unavailable():
    db_service = _FakeDbService(
        experiment=SimpleNamespace(
            status="completed",
            config={"assigned_runner_id": "runner-9", "parameters": {}},
        )
    )
    instance_service = _FakeRunnerInstanceService(allocated_instance=None, runner_status="BUSY")
    service_manager = _FakeResultServiceManager(db_service, instance_service)
    previous = result_routes._service_manager
    result_routes._service_manager = service_manager

    try:
        response = await result_routes.upload_to_memory("exp_demo")
        payload = json.loads(response.body)

        assert response.status_code == 503
        assert "Original runner 'runner-9' is not available" in payload["error"]
        assert instance_service.allocate_calls == [("runner-9", "exp_demo")]
        assert instance_service.release_calls == []
    finally:
        result_routes._service_manager = previous


@pytest.mark.asyncio
async def test_master_managed_runner_start_forwards_memory_api_url_from_loaded_config(monkeypatch: pytest.MonkeyPatch):
    fake_db_service = SimpleNamespace()
    fake_docker = _FakeDockerService()
    fake_config = SimpleNamespace(
        database=SimpleNamespace(url="sqlite+aiosqlite:///:memory:"),
        storage=SimpleNamespace(
            endpoint_url="http://minio:9000",
            access_key="minioadmin",
            secret_key="minioadmin",
        ),
        redis_url="redis://redis:6379/0",
        kafka=SimpleNamespace(enabled=False, bootstrap_servers="kafka:9092"),
        memory_api_url="http://host.docker.internal:8002",
        runner=SimpleNamespace(
            instances={
                "runner-1": SimpleNamespace(host="runner-api-1", port=8001, is_local=True),
            },
            manage_containers=True,
            image_name="runner-image:latest",
            network_name="gigaevo-network",
        ),
    )

    service = RunnerInstanceService(fake_db_service, config=fake_config)
    service.docker_services["runner-1"] = fake_docker

    async def _noop_wait(docker_service, timeout=60):
        return True

    monkeypatch.setattr(service, "_wait_for_container_health", _noop_wait)

    async def _noop_register(instance_id, instance_config, docker_service, derived_status):
        return None

    monkeypatch.setattr(service, "_register_instance_in_db", _noop_register)

    ok = await service.initialize_instance("runner-1")

    assert ok is True
    assert fake_docker.initialize_calls
    env_vars = fake_docker.initialize_calls[0]["environment_vars"]
    assert env_vars["MEMORY_API_URL"] == "http://host.docker.internal:8002"
