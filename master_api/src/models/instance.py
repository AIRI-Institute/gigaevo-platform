#!/usr/bin/env python3

from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class RunnerInstanceStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    INITIALIZING = "initializing"
    READY = "ready"
    BUSY = "busy"
    ERROR = "error"
    TERMINATING = "terminating"


class RunnerInstance(BaseModel):
    id: str
    name: str
    status: RunnerInstanceStatus = RunnerInstanceStatus.OFFLINE
    endpoint_url: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_heartbeat: Optional[datetime] = None
    current_experiment_id: Optional[str] = None
    capabilities: Dict[str, Any] = Field(default_factory=dict)
    resources: Dict[str, Any] = Field(default_factory=dict)
    error_message: Optional[str] = None


class InstanceCreate(BaseModel):
    name: str
    endpoint_url: str
    capabilities: Optional[Dict[str, Any]] = Field(default_factory=dict)


class InstanceUpdate(BaseModel):
    status: Optional[RunnerInstanceStatus] = None
    current_experiment_id: Optional[str] = None
    resources: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
