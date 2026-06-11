"""Docker-CLI-backed sandbox.

Speaks ``docker run`` over subprocess instead of the docker Python SDK so
the runner image stays light (no docker-py dep) and so we can call into
the same Docker socket that's already mounted into the master-api
container for the runner-pool flow.

Defaults are deliberately strict — read-only rootfs, no network, low
resource caps, fixed pids limit. Callers opt out per-request by setting
``RunRequest`` fields.

The container name is ``gigaevo-skill-<short_sha>-<short_uuid>`` — short
SHA keeps traces tied back to the §4.8 cache, short UUID lets the same
skill run concurrently in different containers.
"""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from typing import List, Optional

from .protocol import NetworkPolicy, RunRequest, RunResult, SandboxBackend, SandboxError


def _short(sha: str, n: int = 12) -> str:
    return sha[:n]


def build_docker_run_args(
    req: RunRequest,
    container_name: str,
    *,
    proxy_url: Optional[str] = None,
) -> List[str]:
    """Compose the ``docker run`` argument list for ``req``.

    When ``proxy_url`` is supplied (typically the live URL of a
    per-run :class:`EgressProxy`), the ``SKILL_DECLARED`` policy
    switches from the ``--add-host``-sentinel mode used by §4.5b to a
    proper egress flow: the container joins ``--network bridge`` with
    ``HTTP_PROXY``/``HTTPS_PROXY``/``NO_PROXY`` env vars pointing at
    ``host.docker.internal``-resolved proxy. The proxy enforces the
    SKILL.md allowlist on the host side, so the container can only
    reach whatever the SKILL.md declared in ``WebFetch(domain:*)``.

    Split out so unit tests can assert composition without spawning a
    subprocess. The order is irrelevant for ``docker``; we keep it
    stable so snapshot diffs stay small.
    """
    args: List[str] = [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(req.pids_limit),
        "--cpus",
        f"{req.cpu_limit}",
        "--memory",
        f"{req.memory_limit_mb}m",
        # Workspace mount — read-write so the skill can leave artefacts in
        # ``out/``; bind-mount path is the same inside and outside for
        # predictable error messages.
        "--volume",
        f"{req.workspace}:/workspace:rw",
        "--workdir",
        "/workspace",
    ]

    # Network policy
    if req.network == NetworkPolicy.NONE:
        args.extend(["--network", "none"])
    elif req.network == NetworkPolicy.SKILL_DECLARED:
        if proxy_url:
            # Container reaches the host-side egress proxy via the magic
            # ``host.docker.internal`` DNS name. The proxy is the ONLY
            # network endpoint we expose; the allowlist is enforced
            # there, not via add-host sentinels.
            proxy_for_container = _rewrite_localhost_for_container(proxy_url)
            args.extend(
                [
                    "--network",
                    "bridge",
                    "--add-host",
                    "host.docker.internal:host-gateway",
                ]
            )
            # The env-var sandwich below puts these BEFORE the
            # caller-supplied env so the user can override a single
            # variable if they really need to. The default no-proxy
            # excludes ``host.docker.internal`` so the SDK doesn't try
            # to CONNECT to the proxy *through* the proxy.
            req_env = dict(req.env)
            req_env.setdefault("HTTP_PROXY", proxy_for_container)
            req_env.setdefault("HTTPS_PROXY", proxy_for_container)
            req_env.setdefault("http_proxy", proxy_for_container)
            req_env.setdefault("https_proxy", proxy_for_container)
            req_env.setdefault("NO_PROXY", "localhost,127.0.0.1")
            for key, value in sorted(req_env.items()):
                args.extend(["--env", f"{key}={value}"])
        else:
            # Legacy fail-closed mode from §4.5a/§4.5b: no proxy → every
            # allowed host resolves to the loopback sentinel so the
            # container CANNOT reach the real internet. Real deployments
            # set proxy_url; tests of the legacy mode still pass.
            args.extend(["--network", "bridge", "--dns", "127.0.0.1"])
            for host in req.allowed_domains:
                args.extend(["--add-host", f"{host}:127.0.0.1"])
    elif req.network == NetworkPolicy.OPEN:
        # Documented escape hatch. Still NOT host-network; bridge with
        # default DNS is the most-isolated "fully online" option Docker
        # gives us without extra config.
        args.extend(["--network", "bridge"])
    else:  # defensive — Protocol-checked Enums make this unreachable
        raise SandboxError(f"unknown network policy: {req.network!r}")

    # Skill-bookkeeping labels (visible in ``docker inspect``).
    args.extend(
        [
            "--label",
            f"gigaevo.skill_sha256={req.skill_sha256}",
            "--label",
            f"gigaevo.skill_name={req.skill_name}",
            "--label",
            "gigaevo.kind=agent_skill",
        ]
    )

    # When SKILL_DECLARED + proxy_url applied above, env vars were
    # already emitted (with proxy keys merged in). Skip the duplicate
    # pass in that branch.
    if not (req.network == NetworkPolicy.SKILL_DECLARED and proxy_url):
        for key, value in sorted(req.env.items()):
            args.extend(["--env", f"{key}={value}"])

    args.append(req.image)
    args.extend(req.command)
    return args


def _rewrite_localhost_for_container(proxy_url: str) -> str:
    """Containers can't reach the runner's loopback directly.

    Replace a literal ``127.0.0.1`` / ``localhost`` in the proxy URL
    with ``host.docker.internal`` so the in-container HTTP client
    resolves the proxy via the Docker host gateway. Any other host
    (e.g. an externally-routable proxy address) is left intact.
    """
    for needle in ("//127.0.0.1:", "//localhost:"):
        if needle in proxy_url:
            return proxy_url.replace(needle, "//host.docker.internal:")
    return proxy_url


class DockerSandboxBackend(SandboxBackend):
    name = "docker"
    unsafe = False

    def __init__(self, docker_binary: str = "docker") -> None:
        self._docker = docker_binary

    async def is_available(self) -> bool:
        """``docker version`` reaches the daemon and returns 0."""
        if shutil.which(self._docker) is None:
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                self._docker,
                "version",
                "--format",
                "{{.Server.Version}}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                return False
            return proc.returncode == 0
        except FileNotFoundError:
            return False

    def _container_name(self, req: RunRequest) -> str:
        return f"gigaevo-skill-{_short(req.skill_sha256)}-{uuid.uuid4().hex[:8]}"

    async def run(self, req: RunRequest) -> RunResult:
        if not req.workspace.exists():
            raise SandboxError(f"workspace does not exist: {req.workspace}")
        container_name = self._container_name(req)
        args = build_docker_run_args(req, container_name)

        start = time.monotonic()
        timed_out = False
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=req.timeout_seconds
                )
            except asyncio.TimeoutError:
                timed_out = True
                # Ask Docker to stop the container we know the name of.
                await _kill_container(self._docker, container_name)
                stdout_b, stderr_b = await proc.communicate()
        except FileNotFoundError as exc:
            raise SandboxError(f"docker binary not found: {self._docker}") from exc

        return RunResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
            duration_seconds=time.monotonic() - start,
            timed_out=timed_out,
            container_name=container_name,
            backend=self.name,
        )


async def _kill_container(docker_binary: str, name: str) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            docker_binary,
            "kill",
            name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.communicate(), timeout=5)
    except (FileNotFoundError, asyncio.TimeoutError, ProcessLookupError):
        pass
