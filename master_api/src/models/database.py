#!/usr/bin/env python3

import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, event
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class ExperimentModel(Base):
    """SQLAlchemy model for experiments"""

    __tablename__ = "experiments"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")
    config: Mapped[dict] = mapped_column(JSON, nullable=False)
    data_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Stores best result payload, including best program code and artifact S3 keys
    best_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Human-friendly non-error status text (e.g., queued reason)
    status_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    tasks: Mapped[list["TaskModel"]] = relationship(back_populates="experiment", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<ExperimentModel(id={self.id}, name='{self.name}', status='{self.status}')>"

    def to_dict(self) -> Dict[str, Any]:
        """Convert model to dictionary"""
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "config": self.config,
            "data_path": self.data_path,
            "metrics": self.metrics or {},
            "best_result": self.best_result or {},
            "error_message": self.error_message,
            "status_message": self.status_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class RunnerInstanceModel(Base):
    """SQLAlchemy model for runner instances"""

    __tablename__ = "runner_instances"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    endpoint_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="offline")

    # Resource information
    capabilities: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    resources: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Heartbeat and status
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_experiment_id: Mapped[str | None] = mapped_column(String(255), ForeignKey("experiments.id"), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    current_experiment: Mapped[Optional["ExperimentModel"]] = relationship(backref="assigned_runner")

    @property
    def runner_id(self):
        """Alias for id to maintain compatibility with existing code"""
        return self.id

    def __repr__(self):
        return f"<RunnerInstanceModel(id='{self.id}', status='{self.status}')>"


class TaskModel(Base):
    """SQLAlchemy model for tasks"""

    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    experiment_id: Mapped[str] = mapped_column(String(255), ForeignKey("experiments.id"), nullable=False)
    task_type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")

    # Task configuration and results
    parameters: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    experiment: Mapped["ExperimentModel"] = relationship(back_populates="tasks")

    def __repr__(self):
        return f"<TaskModel(id={self.id}, type='{self.task_type}', status='{self.status}')>"


class FileUploadModel(Base):
    """SQLAlchemy model for file uploads"""

    __tablename__ = "file_uploads"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Upload metadata
    upload_source: Mapped[str] = mapped_column(String(50), nullable=False)
    upload_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self):
        return f"<FileUploadModel(filename='{self.original_filename}', path='{self.storage_path}')>"


# Event listeners for automatic timestamp updates
@event.listens_for(ExperimentModel, "before_update")
def update_experiment_timestamp(mapper, connection, target):
    """Automatically update updated_at timestamp"""
    target.updated_at = func.now()
