#!/usr/bin/env python3

import os
from typing import Optional

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class RedisConfig(BaseModel):
    url: str = "redis://localhost:6379/0"
    max_connections: int = 10


class LLMConfig(BaseModel):
    provider: str = "local-inference"
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: str = "gigachat-max-2"
    max_tokens: int = 4000
    temperature: float = 0.7


class StorageConfig(BaseModel):
    endpoint_url: str = "http://localhost:9000"
    access_key: str = "minioadmin"
    secret_key: str = "minioadmin"
    bucket_name: str = "gigaevo-data"


class WorkerConfig(BaseModel):
    max_workers: int = 3
    worker_timeout: int = 3600
    polling_interval: int = 5
    autostart: bool = False
    autostart_worker_id: str = "dev-worker-1"
    autostart_worker_name: str = "dev-local-1"


class GigaEvolveConfig(BaseModel):
    repo_url: str = "https://github.com/FusionBrainLab/gigaevo-core"
    repo_ref: Optional[str] = "v1.1.0"
    repo_force_refresh: bool = False
    redis_url: str = "redis://redis-gigavolve:6379/0"
    clone_path: str = "./repos/gigaevo-core"
    python_path: str = "python3"
    experiment_timeout: int = 7200
    github_pat: Optional[str] = None
    git_user_name: str = ""
    git_user_email: str = ""
    ssl_bypass_enabled: bool = False
    results_collection_interval: int = 10


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_nested_delimiter="__", extra="ignore")

    # Redis for task queue
    redis: RedisConfig = RedisConfig()

    # LLM configuration
    llm: LLMConfig = LLMConfig()

    # Storage configuration
    storage: StorageConfig = StorageConfig()

    # Worker configuration
    worker: WorkerConfig = WorkerConfig()

    # GigaEvolve configuration
    gigavolve: GigaEvolveConfig = GigaEvolveConfig()

    # Runner API
    host: str = "0.0.0.0"
    port: int = 8001
    debug: bool = False


def load_config(env_file: Optional[str] = None) -> Config:
    env_file = env_file or os.getenv("ENV_FILE")
    return Config(_env_file=env_file)  # type: ignore
