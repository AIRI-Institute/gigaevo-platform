#!/usr/bin/env python3

"""
Generate a docker-compose override file defining a pool of Runner API services.

Design goals:
- Deterministic service names: runner-api-1..N (Compose network addressable)
- Per-runner Redis DB isolation: REDIS__URL and GIGAVOLVE__REDIS_URL point to distinct DB indexes
- Baked gigaevo-core usage:
  - all modes use GIGAVOLVE__CLONE_PATH=/opt/gigaevo-core
  - no runtime repo clone/cache volume mounts

This script intentionally avoids non-stdlib dependencies.
"""

from __future__ import annotations

import argparse
import json
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


def build_compose_config(mode: str, pool_size: int, db_start: int) -> dict[str, object]:
    if pool_size < 1:
        pool_size = 1
    if db_start < 0:
        db_start = 0

    config: dict[str, object] = {"services": {}}
    services: dict[str, object] = config["services"]  # type: ignore[assignment]

    for i in range(1, pool_size + 1):
        svc = f"runner-api-{i}"
        redis_db = db_start + (i - 1)
        service: dict[str, object] = {
            "image": "${RUNNER_IMAGE_NAME:?RUNNER_IMAGE_NAME is required}",
            "env_file": [".env"],
            "environment": [
                f"REDIS__URL=redis://redis:6379/{redis_db}",
                "STORAGE__ENDPOINT_URL=http://minio:9000",
                "STORAGE__ACCESS_KEY=${MINIO_ROOT_USER:-minioadmin}",
                "STORAGE__SECRET_KEY=${MINIO_ROOT_PASSWORD:-minioadmin}",
                f"GIGAVOLVE__REDIS_URL=redis://redis-gigavolve:6379/{redis_db}",
                "MEMORY_API_URL=${MEMORY_API_URL:-http://host.docker.internal:8002}",
            ],
            "extra_hosts": ["host.docker.internal:host-gateway"],
            "depends_on": {
                "redis": {"condition": "service_healthy"},
                "redis-gigavolve": {"condition": "service_healthy"},
                "minio": {"condition": "service_healthy"},
            },
            "healthcheck": {
                "test": ["CMD", "curl", "-f", "http://localhost:8001/health"],
                "interval": "5s",
                "timeout": "10s",
                "retries": 3,
                "start_period": "5s",
            },
        }

        if i == 1:
            service["ports"] = ["${RUNNER_API_HOST_PORT:-8001}:8001"]

        if mode == "dev":
            service["environment"] = list(service["environment"]) + [
                "DEBUG=true",
                "HOST_UID=${HOST_UID:-1000}",
                "HOST_GID=${HOST_GID:-1000}",
                "GIGAVOLVE__CLONE_PATH=/opt/gigaevo-core",
                "PYTHONPYCACHEPREFIX=/tmp/pycache",
            ]
            service["volumes"] = [
                "./runner_api/src:/app/src",
                "input_data:/app/input_data",
                "./llm_models.yml:/llm_models.yml:ro",
                "./common:/app/common:ro",
            ]
            service["command"] = [
                "/app/.venv/bin/uvicorn",
                "src.main:app",
                "--host",
                "0.0.0.0",
                "--port",
                "8001",
                "--reload",
                "--reload-dir",
                "/app/src",
            ]
        else:
            service["environment"] = list(service["environment"]) + ["GIGAVOLVE__CLONE_PATH=/opt/gigaevo-core"]
            service["volumes"] = ["input_data:/app/input_data", "./llm_models.yml:/llm_models.yml:ro"]
            service["networks"] = ["gigaevo-network"]

        services[svc] = service

    if mode != "dev":
        config["networks"] = {"gigaevo-network": {"external": True, "name": "${GIGAEVO_NETWORK_NAME:-gigaevo-network}"}}

    return config


def _render_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))


def _render_yaml(value: object, indent: int = 0) -> list[str]:
    prefix = " " * indent

    if isinstance(value, dict):
        if not value:
            return [f"{prefix}{{}}"]
        lines: list[str] = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                lines.append(f"{prefix}{key}:")
                lines.extend(_render_yaml(item, indent + 2))
            else:
                lines.append(f"{prefix}{key}: {_render_scalar(item)}")
        return lines

    if isinstance(value, list):
        if not value:
            return [f"{prefix}[]"]
        lines = []
        for item in value:
            if isinstance(item, (dict, list)):
                lines.append(f"{prefix}-")
                lines.extend(_render_yaml(item, indent + 2))
            else:
                lines.append(f"{prefix}- {_render_scalar(item)}")
        return lines

    return [f"{prefix}{_render_scalar(value)}"]


def generate(mode: str, pool_size: int, db_start: int) -> str:
    config = build_compose_config(mode, pool_size=pool_size, db_start=db_start)
    return "\n".join(_render_yaml(config)) + "\n"


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
