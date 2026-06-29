"""Sandbox abstraction for the ``RUN_AGENT_SKILL`` runner task.

Three pieces:

* :class:`~src.sandbox.protocol.SandboxBackend` — the contract every backend
  implements; consumed by :class:`runner_api.tasks` once §4.5b lands.
* :class:`~src.sandbox.local_backend.LocalSandboxBackend` — host-subprocess
  fallback. **Provides no isolation**; flagged ``unsafe=True`` so callers can
  refuse it for untrusted skills.
* :class:`~src.sandbox.docker_backend.DockerSandboxBackend` — `docker run` on a
  ``python:3.12-slim`` base with ``--network none`` by default, resource caps
  and a read-only rootfs.

Network-policy plumbing for the ``skill_declared`` mode lives in
:mod:`src.sandbox.network_policy` so MAGE / runner / sandbox can share the
parsing logic.
"""

from .docker_backend import DockerSandboxBackend
from .egress_proxy import AllowList, EgressProxy, EgressProxyError
from .local_backend import LocalSandboxBackend
from .network_policy import NetworkPolicy, extract_allowed_domains
from .protocol import RunRequest, RunResult, SandboxBackend, SandboxError

__all__ = [
    "AllowList",
    "DockerSandboxBackend",
    "EgressProxy",
    "EgressProxyError",
    "LocalSandboxBackend",
    "NetworkPolicy",
    "RunRequest",
    "RunResult",
    "SandboxBackend",
    "SandboxError",
    "extract_allowed_domains",
]
