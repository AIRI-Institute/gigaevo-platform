#!/usr/bin/env python3

from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskType(str, Enum):
    CLONE_REPO = "clone_repo"
    GENERATE_CODE = "generate_code"
    RUN_EXPERIMENT = "run_experiment"
    COLLECT_RESULTS = "collect_results"


class Task(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    experiment_id: str
    task_type: TaskType
    status: TaskStatus = TaskStatus.QUEUED
    parameters: Dict[str, Any] = Field(default_factory=dict)
    result: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    worker_id: Optional[str] = None
    progress: float = 0.0


class TaskCreate(BaseModel):
    experiment_id: str
    task_type: TaskType
    parameters: Dict[str, Any] = Field(default_factory=dict)


class TaskUpdate(BaseModel):
    status: Optional[TaskStatus] = None
    result: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    progress: Optional[float] = None
    worker_id: Optional[str] = None
