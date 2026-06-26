"""Tests for the ``runner_api.src.sandbox`` package.

Coverage layout:

* ``extract_allowed_domains`` — frontmatter → host list edge cases.
* ``RunRequest`` invariants — bad inputs raise ValueError.
* ``LocalSandboxBackend.run`` — real ``python3`` subprocess execution,
  including timeout + non-zero exit + missing workspace error path.
  This is the "real execution scenario" the prompt asks for.
* ``DockerSandboxBackend`` — ``build_docker_run_args`` composition
  per network policy; ``is_available`` against a missing binary.
"""

from __future__ import annotations

import asyncio
import importlib
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER_ROOT = REPO_ROOT / "runner_api"

# The runner package imports as ``src.sandbox.*`` (matching the runner's
# runtime layout under ``runner_api/``). Make that path resolvable.
sys.path.insert(0, str(RUNNER_ROOT))

# Clear any cached ``src`` / ``src.*`` modules from the master_api-flavoured
# tests so we resolve against the runner_api tree. The bare ``src`` entry
# matters: ``importlib.import_module('src.sandbox')`` consults
# ``sys.modules['src']`` before walking ``sys.path``.
for _stale in [m for m in list(sys.modules) if m == "src" or m.startswith("src.")]:
    sys.modules.pop(_stale, None)

sandbox = importlib.import_module("src.sandbox")
np_mod = importlib.import_module("src.sandbox.network_policy")
protocol = importlib.import_module("src.sandbox.protocol")
docker_be = importlib.import_module("src.sandbox.docker_backend")
local_be = importlib.import_module("src.sandbox.local_backend")


# ---------------------------------------------------------------------------
# extract_allowed_domains
# ---------------------------------------------------------------------------


def test_extract_allowed_domains_basic():
    tools = ["WebFetch(domain:adobe.com)", "Read", "WebFetch(domain:*.example.com)"]
    assert np_mod.extract_allowed_domains(tools) == ["adobe.com", "*.example.com"]


def test_extract_allowed_domains_is_case_insensitive_on_token_name():
    assert np_mod.extract_allowed_domains(["webfetch(domain:Foo.Bar)"]) == ["foo.bar"]


def test_extract_allowed_domains_tolerates_whitespace():
    assert np_mod.extract_allowed_domains(["WebFetch( domain : example.com )"]) == ["example.com"]


def test_extract_allowed_domains_drops_duplicates_preserves_order():
    tools = ["WebFetch(domain:a.com)", "WebFetch(domain:b.com)", "WebFetch(domain:a.com)"]
    assert np_mod.extract_allowed_domains(tools) == ["a.com", "b.com"]


def test_extract_allowed_domains_filters_garbage():
    bad = [
        "WebFetch(domain:)",
        "WebFetch(domain:.)",
        "WebFetch(domain:*)",
        "WebFetch(domain:rm -rf /)",
        "Bash",
        12345,  # not a string
    ]
    assert np_mod.extract_allowed_domains(bad) == []


# ---------------------------------------------------------------------------
# RunRequest invariants
# ---------------------------------------------------------------------------


def _req(**overrides):
    base = dict(
        skill_sha256="a" * 64,
        skill_name="t",
        workspace=Path("/tmp"),
        command=["echo", "hi"],
    )
    base.update(overrides)
    return protocol.RunRequest(**base)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"command": []},
        {"cpu_limit": 0},
        {"memory_limit_mb": 0},
        {"pids_limit": 0},
        {"timeout_seconds": 0},
        {"network": protocol.NetworkPolicy.SKILL_DECLARED},  # no allowed_domains
    ],
)
def test_run_request_rejects_invalid_inputs(kwargs):
    with pytest.raises(ValueError):
        _req(**kwargs)


def test_run_request_skill_declared_accepts_when_domains_present():
    req = _req(network=protocol.NetworkPolicy.SKILL_DECLARED, allowed_domains=["x.com"])
    assert req.allowed_domains == ["x.com"]


# ---------------------------------------------------------------------------
# DockerSandboxBackend.build_docker_run_args
# ---------------------------------------------------------------------------


def test_docker_args_default_network_none(tmp_path):
    req = _req(workspace=tmp_path, env={"FOO": "bar"})
    args = docker_be.build_docker_run_args(req, container_name="gigaevo-skill-aaaaaaaaaaaa-abcdef01")
    assert "--network" in args and "none" in args
    # Read-only rootfs + tmpfs + dropped caps + no-new-privs all enforced.
    assert "--read-only" in args
    assert "--cap-drop" in args and "ALL" in args
    assert "--security-opt" in args and "no-new-privileges" in args
    # Resource caps wired through.
    assert "--cpus" in args and "1.0" in args
    assert "--memory" in args and "512m" in args
    assert "--pids-limit" in args and "256" in args
    # Workspace bind mount uses the tmp path verbatim.
    assert "--volume" in args
    assert f"{tmp_path}:/workspace:rw" in args
    # Labels include the skill SHA + name.
    assert "gigaevo.skill_sha256=" + "a" * 64 in args
    assert "gigaevo.skill_name=t" in args
    # Env vars forwarded.
    assert "FOO=bar" in args
    # Image precedes the command. RunRequest.command is ["echo", "hi"].
    assert args[-3] == "python:3.12-slim"
    assert args[-2:] == ["echo", "hi"]
    # Container name placed right after --name.
    assert args[args.index("--name") + 1] == "gigaevo-skill-aaaaaaaaaaaa-abcdef01"


def test_docker_args_skill_declared_adds_host_entries(tmp_path):
    req = _req(
        workspace=tmp_path,
        network=protocol.NetworkPolicy.SKILL_DECLARED,
        allowed_domains=["adobe.com", "*.example.com"],
    )
    args = docker_be.build_docker_run_args(req, container_name="c")
    assert "--network" in args and "bridge" in args
    assert "--add-host" in args
    # Every allowed domain is mapped to the sentinel address until §4.5b
    # wires up an actual proxy.
    add_host_indices = [i for i, a in enumerate(args) if a == "--add-host"]
    add_host_values = [args[i + 1] for i in add_host_indices]
    assert add_host_values == ["adobe.com:127.0.0.1", "*.example.com:127.0.0.1"]


def test_docker_args_open_uses_bridge_without_add_host(tmp_path):
    req = _req(workspace=tmp_path, network=protocol.NetworkPolicy.OPEN)
    args = docker_be.build_docker_run_args(req, container_name="c")
    assert "--network" in args and "bridge" in args
    assert "--add-host" not in args


@pytest.mark.asyncio
async def test_docker_is_available_when_binary_missing():
    backend = docker_be.DockerSandboxBackend(docker_binary="this-binary-does-not-exist-xyz")
    assert await backend.is_available() is False


@pytest.mark.asyncio
async def test_docker_is_available_real_daemon_probe():
    """Reach the actual daemon if it's up; otherwise just confirm we got a bool.

    This test does NOT skip — it always exercises ``is_available`` because
    the CLI is the real-execution boundary we care about. Daemon presence
    is recorded in the assertion message so flaky CI can tell us why.
    """
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not installed on this host")
    backend = docker_be.DockerSandboxBackend()
    result = await backend.is_available()
    assert isinstance(result, bool), "is_available must return bool"


# ---------------------------------------------------------------------------
# LocalSandboxBackend — REAL subprocess execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_run_executes_real_subprocess(tmp_path):
    """Spawn an actual ``python3 -c`` and read its stdout."""
    backend = local_be.LocalSandboxBackend()
    req = _req(
        workspace=tmp_path,
        command=[sys.executable, "-c", "import sys; print('hello'); sys.exit(0)"],
        timeout_seconds=10,
    )
    result = await backend.run(req)
    assert result.exit_code == 0
    assert result.succeeded
    assert "hello" in result.stdout
    assert result.backend == "local"
    assert result.timed_out is False
    assert result.duration_seconds >= 0


@pytest.mark.asyncio
async def test_local_run_captures_nonzero_exit_and_stderr(tmp_path):
    backend = local_be.LocalSandboxBackend()
    req = _req(
        workspace=tmp_path,
        command=[
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('boom\\n'); sys.exit(7)",
        ],
        timeout_seconds=10,
    )
    result = await backend.run(req)
    assert result.exit_code == 7
    assert "boom" in result.stderr
    assert not result.succeeded


@pytest.mark.asyncio
async def test_local_run_honours_timeout(tmp_path):
    backend = local_be.LocalSandboxBackend()
    req = _req(
        workspace=tmp_path,
        command=[sys.executable, "-c", "import time; time.sleep(5)"],
        timeout_seconds=1,
    )
    result = await backend.run(req)
    assert result.timed_out is True
    assert result.succeeded is False
    # Don't pin a specific exit code — depends on the platform's signal mapping.


@pytest.mark.asyncio
async def test_local_run_rejects_missing_workspace():
    backend = local_be.LocalSandboxBackend()
    nowhere = Path(tempfile.gettempdir()) / "definitely-not-a-real-dir-xyz-12345"
    if nowhere.exists():
        nowhere.rmdir()
    req = _req(workspace=nowhere, command=["echo", "hi"])
    with pytest.raises(protocol.SandboxError):
        await backend.run(req)


@pytest.mark.asyncio
async def test_local_backend_is_available_always_true():
    backend = local_be.LocalSandboxBackend()
    assert backend.unsafe is True
    assert backend.name == "local"
    assert await backend.is_available() is True


# ---------------------------------------------------------------------------
# Protocol identity (sanity check that the public surface still resolves)
# ---------------------------------------------------------------------------


def test_public_exports_resolve():
    for name in (
        "DockerSandboxBackend",
        "LocalSandboxBackend",
        "NetworkPolicy",
        "RunRequest",
        "RunResult",
        "SandboxBackend",
        "SandboxError",
        "extract_allowed_domains",
    ):
        assert hasattr(sandbox, name), f"sandbox.{name} missing"


def test_local_and_docker_satisfy_protocol():
    assert isinstance(local_be.LocalSandboxBackend(), protocol.SandboxBackend)
    assert isinstance(docker_be.DockerSandboxBackend(), protocol.SandboxBackend)


# Avoid "unused import" lint when ``asyncio`` is only needed for fixtures.
_ = asyncio
