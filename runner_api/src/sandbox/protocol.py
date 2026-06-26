"""Shared types for the sandbox abstraction."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Protocol, runtime_checkable


class NetworkPolicy(str, Enum):
    """Coarse-grained network behaviour the caller asks the sandbox to apply.

    The string values match the SKILL.md / CARE config vocabulary so we
    can round-trip a policy through JSON without translation.
    """

    NONE = "none"  # ``--network none`` (default)
    SKILL_DECLARED = "skill_declared"  # only domains listed in SKILL.md allowed-tools
    OPEN = "open"  # ``--network host`` — escape hatch, use sparingly


class SandboxError(Exception):
    """Base error for sandbox failures (wrong config, daemon down, etc.)."""


@dataclass(frozen=True)
class RunRequest:
    """Single sandboxed execution.

    The skill payload itself is content-addressed by ``skill_sha256``; the
    bytes are expected to live in MinIO at
    ``agent_skills/<sha256>/SKILL.md`` (populated by the §4.8 resolver).
    """

    skill_sha256: str
    skill_name: str
    workspace: Path
    command: List[str]
    env: Dict[str, str] = field(default_factory=dict)
    cpu_limit: float = 1.0  # whole CPUs (Docker --cpus)
    memory_limit_mb: int = 512  # MiB (Docker --memory)
    pids_limit: int = 256  # Docker --pids-limit
    timeout_seconds: int = 60
    network: NetworkPolicy = NetworkPolicy.NONE
    allowed_domains: List[str] = field(default_factory=list)
    image: str = "python:3.12-slim"

    def __post_init__(self) -> None:
        if not self.command:
            raise ValueError("RunRequest.command must be non-empty")
        if self.cpu_limit <= 0:
            raise ValueError("cpu_limit must be > 0")
        if self.memory_limit_mb <= 0:
            raise ValueError("memory_limit_mb must be > 0")
        if self.pids_limit <= 0:
            raise ValueError("pids_limit must be > 0")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        if self.network == NetworkPolicy.SKILL_DECLARED and not self.allowed_domains:
            raise ValueError(
                "NetworkPolicy.SKILL_DECLARED requires a non-empty allowed_domains list"
            )


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    container_name: Optional[str] = None  # set by Docker backend
    backend: str = "unknown"

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@runtime_checkable
class SandboxBackend(Protocol):
    """Contract every backend implements."""

    name: str
    unsafe: bool  # ``True`` when the backend provides no real isolation

    async def run(self, req: RunRequest) -> RunResult:
        """Execute ``req.command`` against the workspace and return the result."""
        ...

    async def is_available(self) -> bool:
        """Cheap liveness probe (e.g. ``docker version`` for the Docker backend)."""
        ...
