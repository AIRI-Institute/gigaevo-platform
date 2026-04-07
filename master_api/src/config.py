#!/usr/bin/env python3

import os
from typing import Dict, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseConfig(BaseModel):
    url: str = "sqlite+aiosqlite:///:memory:"
    pool_size: int = 10
    max_overflow: int = 20


class StorageConfig(BaseModel):
    endpoint_url: str = "http://localhost:9000"
    access_key: str = "minioadmin"
    secret_key: str = "minioadmin"
    bucket_name: str = "gigaevo-data"


class RunnerInstanceConfig(BaseModel):
    """Configuration for a single RunnerAPI instance"""

    host: str  # Host address (IP or domain)
    port: int = 8001  # Port to run RunnerAPI on
    is_local: bool = False  # Whether this instance runs on the same host as MasterAPI
    ssh_user: Optional[str] = None  # SSH user for remote deployment
    ssh_key_path: Optional[str] = None  # Path to SSH private key for remote deployment
    docker_host: Optional[str] = None  # Docker host for remote Docker daemon


class RunnerConfig(BaseModel):
    """Configuration for RunnerAPI management"""

    max_workers_per_instance: int = 5
    timeout_seconds: int = 3600
    # NOTE: Historically this flag controlled both starting containers and health monitoring.
    # Going forward, container lifecycle and health monitoring are split.
    auto_initialize: bool = True  # Backward-compat only; prefer manage_containers

    # Pool configuration (always used, even when size=1)
    pool_size: int = 1
    pool_host_prefix: str = "runner-api-"

    # Master responsibilities in Compose mode
    manage_containers: bool = True  # When false, Compose starts runners; Master only monitors/allocates.
    health_monitoring_enabled: bool = True  # Must always be ON for pool orchestration.
    reconcile_interval_seconds: int = 15  # Periodic BUSY-runner reconciliation interval

    # Runner status reconciliation hardening:
    # RunnerAPI may briefly return 404 for /status right after /start; avoid premature release.
    missing_status_grace_seconds: int = 30
    status_404_release_threshold: int = 2

    # Queueing settings
    queueing_enabled: bool = True
    queue_poll_interval_seconds: int = 3
    dispatching_ttl_seconds: int = 60

    # Predefined RunnerAPI instances
    instances: Dict[str, RunnerInstanceConfig] = {}

    # Container configuration
    image_name: str = "gigaevo-runner-api:latest"
    container_name_prefix: str = "gigaevo-runner"
    network_name: str = os.getenv("GIGAEVO_NETWORK_NAME", "gigaevo-network")

    # Health check settings
    health_check_interval: int = 15  # seconds
    health_check_timeout: int = 10  # seconds
    max_retries: int = 3

    @property
    def base_url(self) -> str:
        """Legacy property for backward compatibility"""
        # Prefer runner-1 in pooled mode
        inst = self.instances.get("runner-1") or next(iter(self.instances.values()), None)
        if inst:
            return f"http://{inst.host}:{inst.port}"
        return "http://localhost:8001"


class KafkaConfig(BaseModel):
    enabled: bool = True
    bootstrap_servers: str = "localhost:9092"
    group_id: str = "gigaevo-master-group"
    topics: dict = {
        "experiment_config": "experiment-config",
        "experiment_started": "experiment-started",
        "experiment_stopped": "experiment-stopped",
        "runner_status": "runner-status",
    }


class TemplatesConfig(BaseModel):
    """Paths to external templates for experiment constructors."""

    # Absolute or relative path to prompt templates base directory.
    # If provided, Master API will use it instead of internal validate_templates.
    prompt_templates_base: Optional[str] = None


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_nested_delimiter="__", extra="ignore")

    # Database
    database: DatabaseConfig = DatabaseConfig()

    # Storage
    storage: StorageConfig = StorageConfig()

    # Runner API
    runner: RunnerConfig = RunnerConfig()

    # Kafka
    kafka: KafkaConfig = KafkaConfig()

    # Master API
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False

    # Redis (for task queue coordination)
    redis_url: str = "redis://localhost:6379/0"
    timezone: ZoneInfo = ZoneInfo("Europe/Moscow")
    memory_api_url: Optional[str] = None

    # External templates
    templates: TemplatesConfig = TemplatesConfig()


def load_config(env_file: Optional[str] = None) -> Config:
    env_file = env_file or os.getenv("ENV_FILE")
    cfg: Config = Config(_env_file=env_file)  # type: ignore

    # Always treat runners as a pool (even size=1)
    pool_size = int(getattr(cfg.runner, "pool_size", 1) or 1)
    pool_size = max(1, pool_size)
    prefix = str(getattr(cfg.runner, "pool_host_prefix", "runner-api-") or "runner-api-")

    cfg.runner.instances = {
        f"runner-{i}": RunnerInstanceConfig(host=f"{prefix}{i}", port=8001, is_local=True)
        for i in range(1, pool_size + 1)
    }

    # Backward compatibility: if legacy RUNNER__AUTO_INITIALIZE was used, map it to manage_containers
    # while keeping health monitoring always enabled.
    try:
        if hasattr(cfg.runner, "auto_initialize") and cfg.runner.auto_initialize is False:
            cfg.runner.manage_containers = False
    except Exception:
        pass

    cfg.runner.health_monitoring_enabled = True
    return cfg
