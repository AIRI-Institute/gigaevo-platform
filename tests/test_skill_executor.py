"""Tests for ``runner_api.src.services.skill_executor`` + worker dispatch.

Coverage:

* ``SkillExecutionRequest.from_task_parameters`` — payload validation.
* ``SkillExecutor.select_backend`` — every branch of the backend policy
  matrix, including refusal cases.
* ``SkillExecutor.execute`` — **real subprocess** through
  ``LocalSandboxBackend``: SKILL.md lands in the workspace, command can
  read it, stdout flows back, workspace is cleaned up afterwards.
* Timeout path returns ``timed_out=True`` and the dict is well-formed.
* ``TaskWorker._handle_run_agent_skill`` dispatch:
  COMPLETED / FAILED (non-zero exit) / TERMINATED (timeout) /
  FAILED (sandbox refuses).
"""

from __future__ import annotations

import base64
import importlib
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER_ROOT = REPO_ROOT / "runner_api"

# Both: runner_api/ for ``src.*`` imports and repo root for ``common.*``
# imports (``task_worker`` transitively pulls in ``common.llm_registry``).
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(RUNNER_ROOT))

# Clear cached ``src.*`` modules from sibling tests so this file resolves
# against runner_api/src/.
for _stale in [m for m in list(sys.modules) if m == "src" or m.startswith("src.")]:
    sys.modules.pop(_stale, None)

skill_executor_mod = importlib.import_module("src.services.skill_executor")
sandbox_pkg = importlib.import_module("src.sandbox")
config_mod = importlib.import_module("src.config")
task_models = importlib.import_module("src.models.task")
# Cache the worker module HERE — other test files (test_evolutions.py)
# re-bind ``sys.modules['src']`` to master_api during their setup, which
# would otherwise break a later ``importlib.import_module("src.workers...")``.
task_worker_mod = importlib.import_module("src.workers.task_worker")

SkillExecutor = skill_executor_mod.SkillExecutor
SkillExecutionRequest = skill_executor_mod.SkillExecutionRequest
SandboxConfig = config_mod.SandboxConfig
SandboxError = sandbox_pkg.SandboxError
LocalSandboxBackend = sandbox_pkg.LocalSandboxBackend


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


SKILL_MD_BYTES = b"""---
name: pdf-tools
description: Echo back the SKILL.md content.
---
Body.
"""
SKILL_MD_B64 = base64.b64encode(SKILL_MD_BYTES).decode()
SKILL_SHA = "a" * 64


def _params(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "skill_sha256": SKILL_SHA,
        "skill_name": "pdf-tools",
        "skill_md": SKILL_MD_B64,
        # By default the command just reads the materialised SKILL.md and
        # echoes its first line — proves the workspace is wired.
        "command": [sys.executable, "-c", "import pathlib;print(pathlib.Path('SKILL.md').read_text().splitlines()[0])"],
        "network": "none",
    }
    base.update(overrides)
    return base


def _local_only_config(**overrides: Any) -> SandboxConfig:
    return SandboxConfig(
        backend=overrides.pop("backend", "local"),
        unsafe_local_allowed=True,
        default_timeout_seconds=overrides.pop("default_timeout_seconds", 5),
        **overrides,
    )


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------


def test_from_task_parameters_happy_path():
    req = SkillExecutionRequest.from_task_parameters(_params())
    assert req.skill_sha256 == SKILL_SHA
    assert req.skill_name == "pdf-tools"
    assert req.command[0] == sys.executable
    assert req.network == "none"


@pytest.mark.parametrize(
    "mutator",
    [
        lambda p: p.pop("skill_md") or p,  # missing skill_md
        lambda p: (p.update(command=[]) or p),  # empty command
        lambda p: (p.update(command="not a list") or p),
        lambda p: (p.update(env="not a dict") or p),
    ],
)
def test_from_task_parameters_rejects_bad_payloads(mutator):
    bad = mutator(_params())
    with pytest.raises(ValueError):
        SkillExecutionRequest.from_task_parameters(bad)


# ---------------------------------------------------------------------------
# Backend selection matrix
# ---------------------------------------------------------------------------


class _FakeBackend:
    """Minimal SandboxBackend stand-in for select_backend tests."""

    def __init__(self, name: str, available: bool, unsafe: bool = False) -> None:
        self.name = name
        self._available = available
        self.unsafe = unsafe

    async def is_available(self) -> bool:
        return self._available

    async def run(self, _req):  # pragma: no cover — not exercised in select tests
        raise AssertionError("not expected to run in this test")


@pytest.mark.asyncio
async def test_select_local_requires_explicit_unsafe_opt_in():
    cfg = SandboxConfig(backend="local", unsafe_local_allowed=False)
    executor = SkillExecutor(cfg, docker_backend=_FakeBackend("docker", True))
    with pytest.raises(SandboxError, match="unsafe_local_allowed"):
        await executor.select_backend()


@pytest.mark.asyncio
async def test_select_local_with_opt_in_returns_local():
    cfg = SandboxConfig(backend="local", unsafe_local_allowed=True)
    executor = SkillExecutor(cfg, docker_backend=_FakeBackend("docker", True))
    backend = await executor.select_backend()
    assert backend.name == "local"


@pytest.mark.asyncio
async def test_select_docker_when_daemon_up_returns_docker():
    cfg = SandboxConfig(backend="docker")
    executor = SkillExecutor(cfg, docker_backend=_FakeBackend("docker", True))
    backend = await executor.select_backend()
    assert backend.name == "docker"


@pytest.mark.asyncio
async def test_select_docker_when_daemon_down_raises():
    cfg = SandboxConfig(backend="docker")
    executor = SkillExecutor(cfg, docker_backend=_FakeBackend("docker", False))
    with pytest.raises(SandboxError, match="daemon is not reachable"):
        await executor.select_backend()


@pytest.mark.asyncio
async def test_select_auto_prefers_docker_when_available():
    cfg = SandboxConfig(backend="auto", unsafe_local_allowed=True)
    executor = SkillExecutor(cfg, docker_backend=_FakeBackend("docker", True))
    backend = await executor.select_backend()
    assert backend.name == "docker"


@pytest.mark.asyncio
async def test_select_auto_falls_back_to_local_only_if_unsafe_allowed():
    cfg = SandboxConfig(backend="auto", unsafe_local_allowed=True)
    executor = SkillExecutor(cfg, docker_backend=_FakeBackend("docker", False))
    backend = await executor.select_backend()
    assert backend.name == "local"


@pytest.mark.asyncio
async def test_select_auto_refuses_when_docker_down_and_unsafe_off():
    cfg = SandboxConfig(backend="auto", unsafe_local_allowed=False)
    executor = SkillExecutor(cfg, docker_backend=_FakeBackend("docker", False))
    with pytest.raises(SandboxError, match="refusing to run"):
        await executor.select_backend()


@pytest.mark.asyncio
async def test_select_unknown_mode_raises():
    cfg = SandboxConfig(backend="exotic")
    executor = SkillExecutor(cfg, docker_backend=_FakeBackend("docker", True))
    with pytest.raises(SandboxError, match="unknown sandbox.backend"):
        await executor.select_backend()


# ---------------------------------------------------------------------------
# REAL subprocess execution via LocalSandbox
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_runs_command_against_materialised_workspace():
    """The command sees SKILL.md sitting in CWD and exits 0."""
    executor = SkillExecutor(_local_only_config(), local_backend=LocalSandboxBackend())
    req = SkillExecutionRequest.from_task_parameters(_params())
    result = await executor.execute(req)
    assert result["succeeded"] is True
    assert result["exit_code"] == 0
    assert result["backend"] == "local"
    # The command's stdout is the first line of SKILL.md.
    assert result["stdout"].strip() == "---"
    assert result["timed_out"] is False
    # Workspace cleaned up afterwards (default keep_workspace=False).
    assert result["workspace"] is None


@pytest.mark.asyncio
async def test_execute_propagates_nonzero_exit():
    executor = SkillExecutor(_local_only_config(), local_backend=LocalSandboxBackend())
    req = SkillExecutionRequest.from_task_parameters(
        _params(command=[sys.executable, "-c", "import sys;sys.stderr.write('boom\\n');sys.exit(3)"])
    )
    result = await executor.execute(req)
    assert result["exit_code"] == 3
    assert result["succeeded"] is False
    assert "boom" in result["stderr"]


@pytest.mark.asyncio
async def test_execute_honours_timeout():
    executor = SkillExecutor(_local_only_config(default_timeout_seconds=1), local_backend=LocalSandboxBackend())
    req = SkillExecutionRequest.from_task_parameters(
        _params(command=[sys.executable, "-c", "import time;time.sleep(5)"])
    )
    result = await executor.execute(req)
    assert result["timed_out"] is True
    assert result["succeeded"] is False


@pytest.mark.asyncio
async def test_execute_keep_workspace_returns_path_and_skips_cleanup():
    executor = SkillExecutor(_local_only_config(), local_backend=LocalSandboxBackend())
    req = SkillExecutionRequest.from_task_parameters(_params())
    result = await executor.execute(req, keep_workspace=True)
    workspace = Path(result["workspace"])
    try:
        assert workspace.exists()
        assert (workspace / "SKILL.md").read_bytes() == SKILL_MD_BYTES
        assert (workspace / "out").is_dir()
    finally:
        import shutil

        shutil.rmtree(workspace, ignore_errors=True)


@pytest.mark.asyncio
async def test_execute_rejects_bad_base64():
    executor = SkillExecutor(_local_only_config(), local_backend=LocalSandboxBackend())
    req = SkillExecutionRequest.from_task_parameters(_params(skill_md="not!base!64!@@"))
    with pytest.raises(SandboxError, match="not valid base64"):
        await executor.execute(req)


# ---------------------------------------------------------------------------
# TaskWorker._handle_run_agent_skill dispatch
# ---------------------------------------------------------------------------


def _make_worker(skill_executor):
    """Build a TaskWorker without touching Redis / Gigavolve init."""
    return task_worker_mod.TaskWorker(
        worker_id="w-test",
        name="test",
        gigavolve_service=AsyncMock(),
        task_repository=AsyncMock(),
        skill_executor=skill_executor,
    )


def _make_task(parameters: Dict[str, Any]):
    return task_models.Task(
        experiment_id="exp-test",
        task_type=task_models.TaskType.RUN_AGENT_SKILL,
        parameters=parameters,
    )


@pytest.mark.asyncio
async def test_worker_dispatch_completed_on_success():
    executor = SkillExecutor(_local_only_config(), local_backend=LocalSandboxBackend())
    worker = _make_worker(executor)
    task = _make_task(_params())
    await worker._handle_run_agent_skill(task)
    assert task.status == task_models.TaskStatus.COMPLETED
    assert task.result is not None
    assert task.result["succeeded"] is True


@pytest.mark.asyncio
async def test_worker_dispatch_failed_on_nonzero_exit():
    executor = SkillExecutor(_local_only_config(), local_backend=LocalSandboxBackend())
    worker = _make_worker(executor)
    task = _make_task(
        _params(command=[sys.executable, "-c", "import sys;sys.exit(2)"])
    )
    await worker._handle_run_agent_skill(task)
    assert task.status == task_models.TaskStatus.FAILED
    assert task.result["exit_code"] == 2
    assert "code 2" in (task.error_message or "")


@pytest.mark.asyncio
async def test_worker_dispatch_terminated_on_timeout():
    executor = SkillExecutor(_local_only_config(default_timeout_seconds=1), local_backend=LocalSandboxBackend())
    worker = _make_worker(executor)
    task = _make_task(
        _params(command=[sys.executable, "-c", "import time;time.sleep(5)"])
    )
    await worker._handle_run_agent_skill(task)
    assert task.status == task_models.TaskStatus.TERMINATED
    assert task.result["timed_out"] is True


@pytest.mark.asyncio
async def test_worker_dispatch_failed_when_sandbox_refuses():
    """Backend=docker + daemon down → SandboxError → task FAILED, not crash."""
    executor = SkillExecutor(
        SandboxConfig(backend="docker"),
        docker_backend=_FakeBackend("docker", available=False),
    )
    worker = _make_worker(executor)
    task = _make_task(_params())
    await worker._handle_run_agent_skill(task)
    assert task.status == task_models.TaskStatus.FAILED
    assert "Sandbox refused" in (task.error_message or "")
    # No result dict was set because execution never started.
    assert task.result is None


@pytest.mark.asyncio
async def test_worker_dispatch_failed_on_bad_payload():
    executor = SkillExecutor(_local_only_config(), local_backend=LocalSandboxBackend())
    worker = _make_worker(executor)
    task = _make_task({"skill_sha256": "x", "skill_name": "y"})  # missing skill_md + command
    await worker._handle_run_agent_skill(task)
    assert task.status == task_models.TaskStatus.FAILED
    assert "Invalid RUN_AGENT_SKILL payload" in (task.error_message or "")
