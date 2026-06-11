"""Drive a sandboxed SKILL.md execution from a ``RUN_AGENT_SKILL`` task.

Pipeline per task:

1. Decode the SKILL.md bytes from the task payload (sent base64-encoded so
   the JSON channel doesn't mangle frontmatter newlines).
2. Materialise a temp workspace; write the SKILL.md as ``SKILL.md`` and
   create the ``out/`` directory the convention expects.
3. Pick a :class:`~src.sandbox.SandboxBackend` according to
   :class:`~src.config.SandboxConfig`. Docker wins by default; the
   no-isolation Local backend requires an explicit opt-in
   (``unsafe_local_allowed=True``).
4. Build a :class:`~src.sandbox.RunRequest` from caller-supplied params
   (with config defaults filling the gaps) and run it.
5. Hand the :class:`~src.sandbox.RunResult` back as a JSON-serialisable
   dict so the TaskWorker can drop it into ``task.result``.

The class does **not** read SKILL.md from MinIO directly — callers
(TaskWorker or a thin façade) own that. Keeping the executor narrow makes
it trivially testable with the LocalSandbox + a real subprocess, which is
what backs the "real execution scenario" check below.
"""

from __future__ import annotations

import base64
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import SandboxConfig
from ..sandbox import (
    DockerSandboxBackend,
    LocalSandboxBackend,
    NetworkPolicy,
    RunRequest,
    RunResult,
    SandboxBackend,
    SandboxError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillExecutionRequest:
    """Caller-facing input. Mirrors the on-the-wire task ``parameters`` shape."""

    skill_sha256: str
    skill_name: str
    skill_md_b64: str  # base64-encoded SKILL.md bytes
    command: List[str]
    env: Dict[str, str]
    cpu_limit: Optional[float] = None
    memory_limit_mb: Optional[int] = None
    pids_limit: Optional[int] = None
    timeout_seconds: Optional[int] = None
    network: str = "none"  # one of NetworkPolicy values
    allowed_domains: Optional[List[str]] = None
    image: Optional[str] = None

    @classmethod
    def from_task_parameters(cls, params: Dict[str, Any]) -> "SkillExecutionRequest":
        missing = [k for k in ("skill_sha256", "skill_name", "skill_md", "command") if k not in params]
        if missing:
            raise ValueError(f"RUN_AGENT_SKILL task missing required parameters: {missing}")
        if not isinstance(params["command"], list) or not params["command"]:
            raise ValueError("RUN_AGENT_SKILL 'command' must be a non-empty list")
        env = params.get("env") or {}
        if not isinstance(env, dict):
            raise ValueError("RUN_AGENT_SKILL 'env' must be a mapping")
        return cls(
            skill_sha256=str(params["skill_sha256"]),
            skill_name=str(params["skill_name"]),
            skill_md_b64=str(params["skill_md"]),
            command=[str(x) for x in params["command"]],
            env={str(k): str(v) for k, v in env.items()},
            cpu_limit=params.get("cpu_limit"),
            memory_limit_mb=params.get("memory_limit_mb"),
            pids_limit=params.get("pids_limit"),
            timeout_seconds=params.get("timeout_seconds"),
            network=str(params.get("network", "none")),
            allowed_domains=list(params.get("allowed_domains") or []) or None,
            image=params.get("image"),
        )


class SkillExecutor:
    """Coordinator wiring SKILL.md payloads to the sandbox layer."""

    def __init__(
        self,
        config: SandboxConfig,
        docker_backend: Optional[SandboxBackend] = None,
        local_backend: Optional[SandboxBackend] = None,
    ) -> None:
        self._config = config
        self._docker = docker_backend or DockerSandboxBackend()
        self._local = local_backend or LocalSandboxBackend()

    async def select_backend(self) -> SandboxBackend:
        """Resolve :attr:`config.backend` into a concrete backend instance.

        Raises :class:`SandboxError` when the policy cannot be satisfied
        (e.g. ``backend='docker'`` but daemon is down, or ``backend='local'``
        without the explicit unsafe opt-in).
        """
        mode = (self._config.backend or "auto").lower()
        if mode == "local":
            if not self._config.unsafe_local_allowed:
                raise SandboxError(
                    "sandbox.backend='local' requires sandbox.unsafe_local_allowed=True"
                )
            return self._local
        if mode == "docker":
            if not await self._docker.is_available():
                raise SandboxError("sandbox.backend='docker' but the Docker daemon is not reachable")
            return self._docker
        if mode == "auto":
            if await self._docker.is_available():
                return self._docker
            if self._config.unsafe_local_allowed:
                logger.warning(
                    "Docker unavailable; falling back to LocalSandboxBackend "
                    "(no isolation — only safe for trusted skills)"
                )
                return self._local
            raise SandboxError(
                "Docker daemon unavailable and sandbox.unsafe_local_allowed=False — "
                "refusing to run RUN_AGENT_SKILL on the host without isolation"
            )
        raise SandboxError(f"unknown sandbox.backend mode: {mode!r}")

    def _build_run_request(self, req: SkillExecutionRequest, workspace: Path) -> RunRequest:
        try:
            network = NetworkPolicy(req.network)
        except ValueError as exc:
            raise SandboxError(f"invalid network policy: {req.network!r}") from exc
        return RunRequest(
            skill_sha256=req.skill_sha256,
            skill_name=req.skill_name,
            workspace=workspace,
            command=req.command,
            env=req.env,
            cpu_limit=req.cpu_limit if req.cpu_limit is not None else self._config.default_cpu_limit,
            memory_limit_mb=(
                req.memory_limit_mb if req.memory_limit_mb is not None else self._config.default_memory_limit_mb
            ),
            pids_limit=req.pids_limit if req.pids_limit is not None else self._config.default_pids_limit,
            timeout_seconds=(
                req.timeout_seconds if req.timeout_seconds is not None else self._config.default_timeout_seconds
            ),
            network=network,
            allowed_domains=list(req.allowed_domains or []),
            image=req.image or self._config.image,
        )

    def _materialise_workspace(self, req: SkillExecutionRequest) -> Path:
        """Create a temp dir, drop ``SKILL.md`` + ``out/``, return its path."""
        try:
            skill_md = base64.b64decode(req.skill_md_b64, validate=True)
        except (ValueError, TypeError) as exc:
            raise SandboxError(f"skill_md is not valid base64: {exc}") from exc
        workspace = Path(tempfile.mkdtemp(prefix=f"gigaevo-skill-{req.skill_sha256[:12]}-"))
        try:
            (workspace / "SKILL.md").write_bytes(skill_md)
            (workspace / "out").mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            shutil.rmtree(workspace, ignore_errors=True)
            raise SandboxError(f"failed to materialise workspace: {exc}") from exc
        return workspace

    @staticmethod
    def _result_to_dict(result: RunResult) -> Dict[str, Any]:
        return {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration_seconds": result.duration_seconds,
            "timed_out": result.timed_out,
            "container_name": result.container_name,
            "backend": result.backend,
            "succeeded": result.succeeded,
        }

    async def execute(self, req: SkillExecutionRequest, *, keep_workspace: bool = False) -> Dict[str, Any]:
        """Run ``req`` through the selected backend and return a result dict."""
        backend = await self.select_backend()
        workspace = self._materialise_workspace(req)
        try:
            run_request = self._build_run_request(req, workspace)
            result = await backend.run(run_request)
            payload = self._result_to_dict(result)
            payload["workspace"] = str(workspace) if keep_workspace else None
            return payload
        finally:
            if not keep_workspace:
                shutil.rmtree(workspace, ignore_errors=True)
