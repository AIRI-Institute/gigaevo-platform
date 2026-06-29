import tempfile
import types
from pathlib import Path

import pytest

from runner_api.src.services import gigavolve_service as gigavolve_module
from runner_api.src.services.gigavolve_service import GigaEvolveService


class _FakeProcess:
    def __init__(self, *, returncode: int = 0, stdout: bytes = b"ok", stderr: bytes = b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.killed = False

    async def communicate(self):
        return self._stdout, self._stderr

    async def wait(self):
        return self.returncode

    def kill(self):
        self.killed = True


def _fake_config(clone_path: Path):
    return types.SimpleNamespace(
        gigavolve=types.SimpleNamespace(
            clone_path=str(clone_path),
            python_path="python3",
            redis_url="redis://redis-gigavolve:6379/7",
            ssl_bypass_enabled=False,
            experiment_timeout=7200,
            results_collection_interval=10,
        ),
        redis=types.SimpleNamespace(url="redis://redis:6379/0"),
    )


def _prepare_clone(tmp_path: Path) -> Path:
    clone_path = tmp_path / "gigaevo-core"
    (clone_path / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    (clone_path / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    (clone_path / "problems" / "exp_demo").mkdir(parents=True, exist_ok=True)
    (clone_path / "config").mkdir(parents=True, exist_ok=True)
    (clone_path / "config" / "config.yaml").write_text(
        "defaults:\n  - experiment: base\n  - /migration_bus: disabled\n  - ideas_tracker: none\n  - _self_\n",
        encoding="utf-8",
    )
    (clone_path / "config" / "memory.yaml").write_text(
        "ideas_tracker:\n  analyzer:\n    base_url: https://openrouter.ai/api/v1\n",
        encoding="utf-8",
    )
    (clone_path / "run.py").write_text("print('ok')\n", encoding="utf-8")
    return clone_path


def _patch_runtime(monkeypatch: pytest.MonkeyPatch, clone_path: Path):
    cfg = _fake_config(clone_path)
    monkeypatch.setattr(gigavolve_module, "load_config", lambda env_file=None: cfg)
    monkeypatch.setattr(
        gigavolve_module,
        "load_llm_registry",
        lambda: {
            "defaults": {
                "llm_model": "evo-model",
                "prompt_llm_model": "prompt-model",
            },
            "models": [
                {
                    "id": "evo-model",
                    "runtime": {
                        "model": "evo-model",
                        "api_key": "test-key",
                        "base_url": "http://llm.local/v1",
                    },
                },
                {
                    "id": "openrouter-model",
                    "runtime": {
                        "model": "openrouter-model",
                        "api_key": "openrouter-test-key",
                        "base_url": "https://openrouter.ai/api/v1",
                    },
                },
            ],
        },
    )
    monkeypatch.setattr(
        gigavolve_module,
        "get_llm_runtime_required",
        lambda model_id, required_keys=None: {
            "evo-model": {
                "model": "evo-model",
                "api_key": "test-key",
                "base_url": "http://llm.local/v1",
                "temperature": 0.1,
                "max_tokens": 128,
                "top_p": 1.0,
                "max_retries": 2,
                "timeout": 60,
                "request_timeout": 60,
            },
            "prompt-model": {
                "model": "prompt-model",
                "api_key": "prompt-test-key",
                "base_url": "http://prompt.local/v1",
                "temperature": 0.1,
                "max_tokens": 128,
                "top_p": 1.0,
                "max_retries": 2,
                "timeout": 60,
                "request_timeout": 60,
                "openrouter_api_key": "prompt-openrouter-test-key",
            },
            "openrouter-model": {
                "model": "openrouter-model",
                "api_key": "openrouter-test-key",
                "base_url": "https://openrouter.ai/api/v1",
                "temperature": 0.1,
                "max_tokens": 128,
                "top_p": 1.0,
                "max_retries": 2,
                "timeout": 60,
                "request_timeout": 60,
            },
        }[model_id],
    )
    return cfg


@pytest.mark.asyncio
async def test_run_experiment_adds_memory_overrides_and_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    clone_path = _prepare_clone(tmp_path)
    _patch_runtime(monkeypatch, clone_path)
    monkeypatch.setenv("MEMORY_API_URL", "http://memory.local:8000")

    captured = {}

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["kwargs"] = kwargs
        return _FakeProcess()

    monkeypatch.setattr(gigavolve_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    service = GigaEvolveService()
    result = await service.run_experiment(
        "exp_demo",
        {
            "llm_model": "evo-model",
            "prompt_llm_model": "prompt-model",
            "max_iterations": 5,
            "enable_memory": True,
            "memory_namespace": "shared-bank",
        },
    )

    checkpoint_dir = clone_path / "outputs" / "exp_demo" / "memory"
    env = captured["kwargs"]["env"]

    assert result["success"] is True
    assert "+max_generations=5" in captured["cmd"]
    assert "+memory=api" in captured["cmd"]
    assert "namespace=shared-bank" in captured["cmd"]
    assert f"checkpoint_dir={checkpoint_dir}" in captured["cmd"]
    assert env["MEMORY_API_URL"] == "http://memory.local:8000"
    assert env["MEMORY_NAMESPACE"] == "shared-bank"
    assert env["MEMORY_USE_API"] == "true"
    assert captured["kwargs"]["cwd"] == str(clone_path)


@pytest.mark.asyncio
async def test_run_experiment_uses_plain_memory_override_when_core_has_memory_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    clone_path = _prepare_clone(tmp_path)
    (clone_path / "config" / "config.yaml").write_text(
        "defaults:\n"
        "  - experiment: base\n"
        "  - /migration_bus: disabled\n"
        "  - memory: none\n"
        "  - ideas_tracker: none\n"
        "  - _self_\n",
        encoding="utf-8",
    )
    _patch_runtime(monkeypatch, clone_path)
    monkeypatch.setenv("MEMORY_API_URL", "http://memory.local:8000")

    captured = {}

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["kwargs"] = kwargs
        return _FakeProcess()

    monkeypatch.setattr(gigavolve_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    service = GigaEvolveService()
    result = await service.run_experiment(
        "exp_demo",
        {
            "llm_model": "evo-model",
            "prompt_llm_model": "prompt-model",
            "max_iterations": 5,
            "enable_memory": True,
        },
    )

    assert result["success"] is True
    assert "memory=api" in captured["cmd"]
    assert "+memory=api" not in captured["cmd"]


@pytest.mark.asyncio
async def test_run_experiment_fails_fast_when_memory_url_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    clone_path = _prepare_clone(tmp_path)
    _patch_runtime(monkeypatch, clone_path)
    monkeypatch.delenv("MEMORY_API_URL", raising=False)

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("subprocess should not be started without MEMORY_API_URL")

    monkeypatch.setattr(gigavolve_module.asyncio, "create_subprocess_exec", fail_if_called)

    service = GigaEvolveService()
    result = await service.run_experiment(
        "exp_demo",
        {
            "llm_model": "evo-model",
            "prompt_llm_model": "prompt-model",
            "enable_memory": True,
        },
    )

    assert result["success"] is False
    assert "MEMORY_API_URL is required" in str(result["error"])


@pytest.mark.asyncio
async def test_run_ideas_tracker_uses_same_checkpoint_and_namespace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    clone_path = _prepare_clone(tmp_path)
    _patch_runtime(monkeypatch, clone_path)
    monkeypatch.setenv("MEMORY_API_URL", "http://memory.local:8000")

    captured = {}

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["kwargs"] = kwargs
        return _FakeProcess(stdout=b"tracker-ok")

    monkeypatch.setattr(gigavolve_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    service = GigaEvolveService()
    result = await service.run_ideas_tracker("exp_demo", {"memory_namespace": "shared-bank"})

    checkpoint_dir = clone_path / "outputs" / "exp_demo" / "memory"
    env = captured["kwargs"]["env"]

    assert result["success"] is True
    assert captured["cmd"][:3] == [
        str(clone_path / ".venv" / "bin" / "python"),
        "-m",
        "src.tools.run_ideas_tracker_from_redis",
    ]
    assert "--memory-write" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--redis-prefix") + 1] == "demo"
    assert captured["cmd"][captured["cmd"].index("--checkpoint-dir") + 1] == str(checkpoint_dir)
    expected_logs_dir = Path(tempfile.gettempdir()) / "gigaevo_ideas_tracker_logs" / "exp_demo"
    assert captured["cmd"][captured["cmd"].index("--logs-dir") + 1] == str(expected_logs_dir)
    assert env["MEMORY_API_URL"] == "http://memory.local:8000"
    assert env["MEMORY_NAMESPACE"] == "shared-bank"
    assert env["MEMORY_USE_API"] == "true"
    assert env["OPENAI_API_KEY"] == "openrouter-test-key"
    assert env["OPENAI_BASE_URL"] == "https://openrouter.ai/api/v1"


@pytest.mark.asyncio
async def test_run_ideas_tracker_requires_memory_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    clone_path = _prepare_clone(tmp_path)
    _patch_runtime(monkeypatch, clone_path)
    monkeypatch.delenv("MEMORY_API_URL", raising=False)

    service = GigaEvolveService()

    with pytest.raises(ValueError, match="MEMORY_API_URL is required"):
        await service.run_ideas_tracker("exp_demo", {})
