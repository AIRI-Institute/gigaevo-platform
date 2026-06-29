"""Pydantic models for data structures."""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class TaskType(str, Enum):
    """Supported task types."""

    CLASSIFICATION = "classification"
    REGRESSION = "regression"
    CLUSTERING = "clustering"


class InstanceStatus(str, Enum):
    """Instance status values."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    CONFIGURED = "configured"
    DISABLED = "disabled"
    NOT_CONFIGURED = "not_configured"
    ONLINE = "online"
    INITIALIZING = "initializing"
    READY = "ready"
    OFFLINE = "offline"
    ERROR = "error"
    BUSY = "busy"
    TERMINATING = "terminating"
    NO_INSTANCES = "no_instances"
    UNKNOWN = "unknown"


class ExperimentStatus(str, Enum):
    """Experiment status values."""

    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TERMINATED = "terminated"
    STOPPED = "stopped"
    PENDING = "pending"
    UNKNOWN = "unknown"


class SystemStatus(BaseModel):
    """System status information."""

    status: str = InstanceStatus.UNKNOWN.value
    version: str = "unknown"
    uptime_seconds: Optional[int] = None
    components: Dict[str, str] = {}
    timestamp: Optional[str] = None


class Experiment(BaseModel):
    """Experiment information."""

    id: str
    name: str
    status: ExperimentStatus
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    config: Dict[str, Any] = {}
    data_path: Optional[str] = None
    error_message: Optional[str] = None
    metrics: Dict[str, Any] = {}


class Instance(BaseModel):
    """Runner instance information."""

    id: str
    name: str
    status: InstanceStatus
    endpoint_url: str
    created_at: Optional[str] = None
    last_heartbeat: Optional[str] = None
    capabilities: Dict[str, Any] = {}
    resources: Dict[str, Any] = {}
    current_experiment_id: Optional[str] = None


class Example(BaseModel):
    """Example dataset information."""

    name: str
    label: Optional[str] = None
    description: Optional[str] = None
    spec: Dict[str, Any] = {}


class HealthSummary(BaseModel):
    """Instance health summary."""

    total_instances: int = 0
    healthy_instances: int = 0
    unhealthy_instances: int = 0
    busy_instances: int = 0
    ready_instances: int = 0
    offline_instances: int = 0
    instances_detail: List[Instance] = []


class ExperimentSummary(BaseModel):
    """Experiment execution summary."""

    total_iterations: Optional[int] = None
    total_programs: Optional[int] = None
    total_programs_complete: Optional[int] = None
    best_fitness: Optional[float] = None
    best_generations: Optional[int] = None
    best_program: Optional[str] = None
    token_usage: Optional[Dict[str, Any]] = None


class ApiError(BaseModel):
    """API error response."""

    error: str
    details: Optional[Dict[str, Any]] = None


class ApiResponse(BaseModel):
    """Generic API response."""

    success: bool = True
    message: Optional[str] = None
    data: Optional[Any] = None
    error: Optional[str] = None


class ExperimentConfig(BaseModel):
    """Experiment configuration."""

    task_type: TaskType
    task_description: Optional[str] = None
    target_field: Optional[str] = None
    n_classes: Optional[int] = None
    n_clusters: Optional[int] = None
    dataset_path: Optional[str] = None
    llm_model: str = "gigachat-max-2"
    max_iterations: int = 100
    timeout_seconds: int = 3600


class CreateExperimentRequest(BaseModel):
    """Request to create a new experiment."""

    name: str
    config: ExperimentConfig
    data_path: Optional[str] = None


class PresetButtonUpdate(BaseModel):
    """Update for preset button."""

    value: Optional[str] = None
    visible: bool = True
