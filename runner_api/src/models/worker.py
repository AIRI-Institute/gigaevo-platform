#!/usr/bin/env python3

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class WorkerStatus(str, Enum):
    IDLE = "idle"
    BUSY = "busy"
    ERROR = "error"
    OFFLINE = "offline"


class Worker(BaseModel):
    id: str
    name: str
    status: WorkerStatus = WorkerStatus.OFFLINE
    current_task_id: Optional[str] = None
    capabilities: Dict[str, Any] = Field(default_factory=dict)
    resources: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_heartbeat: Optional[datetime] = None
    total_tasks_completed: int = 0
    error_message: Optional[str] = None


class WorkerUpdate(BaseModel):
    status: Optional[WorkerStatus] = None
    current_task_id: Optional[str] = None
    resources: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    total_tasks_completed: Optional[int] = None
