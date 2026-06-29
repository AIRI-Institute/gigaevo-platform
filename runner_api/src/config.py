#!/usr/bin/env python3

import os
import socket
from typing import Optional

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RedisConfig(BaseModel):
    url: str = "redis://localhost:6379/0"
    max_connections: int = 10


class StorageConfig(BaseModel):
    endpoint_url: str = "http://localhost:9000"
    access_key: str = "minioadmin"
    secret_key: str = "minioadmin"
    bucket_name: str = "gigaevo-data"


class WorkerConfig(BaseModel):
    max_workers: int = 3
    worker_timeout: int = 3600
    polling_interval: int = 5
    worker_id: str = Field(default_factory=lambda: f"runner-api-{socket.gethostname()}-{os.getpid()}")


class SandboxConfig(BaseModel):
    """Sandbox backend selection for RUN_AGENT_SKILL tasks (CARE §4.5b)."""

    # ``auto``  → prefer DockerSandboxBackend if the daemon is reachable, fall
    #             back to LocalSandboxBackend only when ``unsafe_local_allowed``
    #             is True. Otherwise raise.
    # ``docker``→ require Docker. Fail closed if the daemon isn't reachable.
    # ``local`` → always use LocalSandboxBackend. Requires
    #             ``unsafe_local_allowed`` because Local provides NO isolation.
    backend: str = "auto"
    unsafe_local_allowed: bool = False
    default_cpu_limit: float = 1.0
    default_memory_limit_mb: int = 512
    default_pids_limit: int = 256
    default_timeout_seconds: int = 60
    image: str = "python:3.12-slim"


class GigaEvolveConfig(BaseModel):
    redis_url: str = "redis://redis-gigavolve:6379/0"
    clone_path: str = "/opt/gigaevo-core"
    python_path: str = "python3"
    experiment_timeout: int = 7200
    ssl_bypass_enabled: bool = False
    results_collection_interval: int = 10


class ExtrasConfig(BaseModel):
    """Optional extra paths/hooks."""

    # Absolute path to prompt_layout.py to copy into cloned repo if missing
    prompt_layout_source: Optional[str] = None


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_nested_delimiter="__", extra="ignore")

    # Redis for task queue
    redis: RedisConfig = RedisConfig()

    # Storage configuration
    storage: StorageConfig = StorageConfig()

    # Worker configuration
    worker: WorkerConfig = WorkerConfig()

    # GigaEvolve configuration
    gigavolve: GigaEvolveConfig = GigaEvolveConfig()

    # Sandbox configuration (RUN_AGENT_SKILL)
    sandbox: SandboxConfig = SandboxConfig()

    # Extras
    extras: ExtrasConfig = ExtrasConfig()

    # Runner API
    host: str = "0.0.0.0"
    port: int = 8001
    debug: bool = False


def load_config(env_file: Optional[str] = None) -> Config:
    env_file = env_file or os.getenv("ENV_FILE")
    return Config(_env_file=env_file)  # type: ignore
