"""Host-subprocess sandbox backend.

Provides **no isolation** — it exists so developers can run skills against
the same machine that hosts the runner during early bring-up, and so unit
tests have a backend that doesn't require Docker. The ``unsafe`` flag is
``True`` so callers can refuse this backend for skills they don't trust.

The implementation runs ``req.command`` with the workspace as CWD, the
caller-supplied env, and a hard ``timeout_seconds`` wall-clock cap.
Resource limits (cpu / memory / pids) are ignored — that's the trade-off
for not requiring Docker.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from .protocol import RunRequest, RunResult, SandboxBackend, SandboxError


class LocalSandboxBackend(SandboxBackend):
    name = "local"
    unsafe = True

    async def is_available(self) -> bool:
        return True

    async def run(self, req: RunRequest) -> RunResult:
        if not req.workspace.exists():
            raise SandboxError(f"workspace does not exist: {req.workspace}")
        if not req.workspace.is_dir():
            raise SandboxError(f"workspace is not a directory: {req.workspace}")

        start = time.monotonic()
        proc: Optional[asyncio.subprocess.Process] = None
        timed_out = False
        try:
            proc = await asyncio.create_subprocess_exec(
                *req.command,
                cwd=str(req.workspace),
                env={**req.env} if req.env else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=req.timeout_seconds
                )
            except asyncio.TimeoutError:
                timed_out = True
                # Best-effort termination; SIGKILL if SIGTERM doesn't take.
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                stdout_b, stderr_b = await proc.communicate()
        except FileNotFoundError as exc:
            raise SandboxError(f"command not found: {req.command[0]}") from exc

        return RunResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
            duration_seconds=time.monotonic() - start,
            timed_out=timed_out,
            backend=self.name,
        )
