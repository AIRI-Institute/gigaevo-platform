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
    auto_initialize: bool = True  # Automatically initialize instances on startup

    # Predefined RunnerAPI instances
    instances: Dict[str, RunnerInstanceConfig] = {
        "local": RunnerInstanceConfig(
            host="runner-api",  # Use Docker service name for containerized environment
            is_local=True,
        ),
        # Example remote instance:
        # "remote-1": RunnerInstanceConfig(
        #     host="192.168.1.100",
        #     is_local=False,
        #     ssh_user="ubuntu",
        #     ssh_key_path="/path/to/key.pem",
        #     docker_host="ssh://ubuntu@192.168.1.100"
        # )
    }

    # Container configuration
    image_name: str = "gigaevo-runner-api:latest"
    container_name_prefix: str = "gigaevo-runner"
    network_name: str = "gigaevo-network"

    # Health check settings
    health_check_interval: int = 30  # seconds
    health_check_timeout: int = 10  # seconds
    max_retries: int = 3

    @property
    def base_url(self) -> str:
        """Legacy property for backward compatibility"""
        local_instance = self.instances.get("local")
        if local_instance:
            return f"http://{local_instance.host}:{local_instance.port}"
        return "http://localhost:8001"


class KafkaConfig(BaseModel):
    enabled: bool = True
    bootstrap_servers: str = "localhost:9092"
    group_id: str = "gigaevo-master-group"
    topics: dict = {
        "experiment_config": "experiment-config",
        "experiment_prepared": "experiment-prepared",
        "experiment_started": "experiment-started",
        "experiment_stopped": "experiment-stopped",
        "runner_status": "runner-status",
    }


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


def load_config(env_file: Optional[str] = None) -> Config:
    env_file = env_file or os.getenv("ENV_FILE")
    return Config(_env_file=env_file)  # type: ignore
