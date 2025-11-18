#!/usr/bin/env python3

from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class ExperimentStatus(str, Enum):
    PENDING = "pending"
    PREPARING = "preparing"
    PREPARED = "prepared"
    INITIALIZING = "initializing"
    DEPLOYED = "deployed"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExperimentConfig(BaseModel):
    description: Optional[str] = None
    parameters: Dict[str, Any] = Field(default_factory=dict)
    llm_model: str = "local-inference"
    max_iterations: int = 100
    timeout_seconds: int = 3600


class ExperimentCreate(BaseModel):
    name: str
    config: ExperimentConfig
    data_path: str


class Experiment(BaseModel):
    id: str = Field(default_factory=lambda: f"exp_{uuid4()}")
    name: str
    status: ExperimentStatus = ExperimentStatus.PENDING
    config: ExperimentConfig
    data_path: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    metrics: Dict[str, Any] = Field(default_factory=dict)
    best_result: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None


class ExperimentUpdate(BaseModel):
    status: Optional[ExperimentStatus] = None
    metrics: Optional[Dict[str, Any]] = None
    best_result: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
