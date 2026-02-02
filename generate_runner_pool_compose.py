#!/usr/bin/env python3

"""
Generate a docker-compose override file defining a pool of Runner API services.

Design goals:
- Deterministic service names: runner-api-1..N (Compose network addressable)
- Per-runner Redis DB isolation: REDIS__URL and GIGAVOLVE__REDIS_URL point to distinct DB indexes
- Workspace isolation:
  - dev: unique GIGAVOLVE__CLONE_PATH per runner under /app/repos (host bind-mounted)
  - deploy: unique GIGAVOLVE__CLONE_PATH per runner under /tmp/gigavolve (named volume)

This script intentionally avoids non-stdlib dependencies.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def _read_dotenv(path: Path) -> dict[str, str]:
    """
    Minimal .env parser: KEY=VALUE lines, supports comments and quoted values.
    This is intentionally minimal and only used for RUNNER_POOL_SIZE/RUNNER_REDIS_DB_START.
    """
    out: dict[str, str] = {}
    if not path.exists():
        return out
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if not k:
                continue
            # Strip optional quotes
            if len(v) >= 2 and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
                v = v[1:-1]
            out[k] = v
    except Exception:
        return out
    return out


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except Exception:
        return default


def _quote(s: str) -> str:
    # YAML-safe quoting for simple strings
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def generate(mode: str, pool_size: int, db_start: int) -> str:
    if pool_size < 1:
        pool_size = 1
    if db_start < 0:
        db_start = 0

    lines: list[str] = []
    lines.append("services:")

    for i in range(1, pool_size + 1):
        svc = f"runner-api-{i}"
        redis_db = db_start + (i - 1)
        # Expose only the first runner for debugging/compat: localhost:8001
        ports = ['      - "8001:8001"'] if i == 1 else []

        # Per-runner named volumes (deploy) to avoid collisions in /tmp caches
        vol_cache = f"gigavolve_cache_runner_{i}"
        vol_problems = f"problems_data_runner_{i}"

        lines.append(f"  {svc}:")
        lines.append("    build:")
        lines.append("      context: .")
        if mode == "dev":
            lines.append("      dockerfile: runner_api/Dockerfile.dev")
        else:
            lines.append("      dockerfile: runner_api/Dockerfile")
        lines.append("    env_file:")
        lines.append("      - .env")
        lines.append("    environment:")
        lines.append(f"      - REDIS__URL=redis://redis:6379/{redis_db}")
        lines.append("      - STORAGE__ENDPOINT_URL=http://minio:9000")
        lines.append("      - STORAGE__ACCESS_KEY=minioadmin")
        lines.append("      - STORAGE__SECRET_KEY=minioadmin")
        lines.append(f"      - GIGAVOLVE__REDIS_URL=redis://redis-gigavolve:6379/{redis_db}")

        if mode == "dev":
            lines.append("      - DEBUG=true")
            lines.append("      - EXTRAS__PROMPT_LAYOUT_SOURCE=/app/master_api/src/folder_constructor/prompt_layout.py")
            lines.append("      - HOST_UID=${HOST_UID:-1000}")
            lines.append("      - HOST_GID=${HOST_GID:-1000}")
            # Isolate checkout/workspace per runner while keeping host-visible repos directory
            lines.append(f"      - GIGAVOLVE__CLONE_PATH=/app/repos/gigaevo-core-{i}")
        else:
            # Keep existing deploy default for prompt layout source if provided by environment/host
            lines.append(
                "      - EXTRAS__PROMPT_LAYOUT_SOURCE=/host/gigaevo-core-internal/gigaevo/problems/prompt_layout.py"
            )
            # Persist cloned repo + repo-local venv across container restarts by placing it under the
            # per-runner named volume mounted at /tmp/gigavolve.
            lines.append(f"      - GIGAVOLVE__CLONE_PATH=/tmp/gigavolve/gigaevo-core-{i}")

        if ports:
            lines.append("    ports:")
            lines.extend(ports)

        lines.append("    depends_on:")
        lines.append("      redis:")
        lines.append("        condition: service_healthy")
        lines.append("      redis-gigavolve:")
        lines.append("        condition: service_healthy")
        lines.append("      minio:")
        lines.append("        condition: service_healthy")

        lines.append("    volumes:")
        if mode == "dev":
            lines.append("      - ./runner_api/src:/app/src")
            lines.append("      - ./runner_api/repos:/app/repos")
            lines.append("      - ./master_api/src:/app/master_api/src:ro")
            lines.append("      - input_data:/app/input_data")
            lines.append("      - ./llm_models.yml:/llm_models.yml:ro")
            lines.append("      - ./common:/app/common:ro")
        else:
            lines.append(f"      - {vol_cache}:/tmp/gigavolve")
            lines.append("      - input_data:/app/input_data")
            lines.append(f"      - {vol_problems}:/tmp/problems")
            lines.append("      - ./llm_models.yml:/llm_models.yml:ro")

        lines.append("    healthcheck:")
        lines.append('      test: ["CMD", "curl", "-f", "http://localhost:8001/health"]')
        lines.append("      interval: 30s")
        lines.append("      timeout: 10s")
        lines.append("      retries: 3")

        if mode != "dev":
            lines.append("    networks:")
            lines.append("      - gigaevo-network")

        if mode == "dev":
            lines.append("    command:")
            lines.append("      [")
            # Use the already-built venv uvicorn directly to avoid runtime `uv run`
            # delays that can keep the container unhealthy for a long time.
            lines.append('        "/app/.venv/bin/uvicorn",')
            lines.append('        "src.main:app",')
            lines.append('        "--host",')
            lines.append('        "0.0.0.0",')
            lines.append('        "--port",')
            lines.append('        "8001",')
            lines.append('        "--reload",')
            lines.append('        "--reload-dir",')
            lines.append('        "/app/src",')
            lines.append("      ]")

    # Declare named volumes used in deploy mode
    if mode != "dev":
        lines.append("")
        lines.append("volumes:")
        for i in range(1, pool_size + 1):
            lines.append(f"  gigavolve_cache_runner_{i}:")
            lines.append(f"  problems_data_runner_{i}:")

        lines.append("")
        lines.append("networks:")
        lines.append("  gigaevo-network:")
        lines.append("    external: true")

    return "\n".join(lines) + "\n"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["dev", "deploy"], required=True)
    p.add_argument("--output", required=True, help="Output compose YAML path")
    args = p.parse_args()

    # Prefer real environment variables, but also read from repo .env for "make dev" convenience.
    dotenv = _read_dotenv(Path(".env"))
    pool_size = _env_int("RUNNER_POOL_SIZE", int(dotenv.get("RUNNER_POOL_SIZE", "1") or "1"))
    db_start = _env_int("RUNNER_REDIS_DB_START", int(dotenv.get("RUNNER_REDIS_DB_START", "1") or "1"))

    out_path = Path(args.output)
    content = generate(args.mode, pool_size=pool_size, db_start=db_start)
    out_path.write_text(content, encoding="utf-8")
    print(f"Wrote runner pool compose: {out_path} (mode={args.mode}, N={pool_size}, db_start={db_start})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
