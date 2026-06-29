#!/usr/bin/env python3

from typing import List, Optional
from uuid import UUID

from ..models.task import Task, TaskCreate, TaskStatus, TaskUpdate
from .task_repository import TaskRepository


class TaskService:
    def __init__(self, repository: Optional[TaskRepository] = None):
        self.repository = repository or TaskRepository()

    async def create_task(self, task: TaskCreate) -> Task:
        """Create a new task and enqueue it"""
        return await self.repository.create_task(task)

    async def list_tasks(self, status: TaskStatus = None, limit: int = 100, offset: int = 0) -> List[Task]:
        """List tasks with optional status filter (best-effort scan)."""
        return await self.repository.list_tasks(status=status, limit=limit, offset=offset)

    async def get_task(self, task_id: UUID) -> Optional[Task]:
        """Get task by ID"""
        return await self.repository.get_task(task_id)

    async def update_task(self, task_id: UUID, task_update: TaskUpdate) -> Optional[Task]:
        """Update task fields"""
        return await self.repository.update_task(task_id, task_update)

    async def delete_task(self, task_id: UUID) -> bool:
        """Delete task and remove from queues"""
        return await self.repository.delete_task(task_id)

    async def cancel_task(self, task_id: UUID) -> bool:
        """Cancel task execution if possible (remove from queue, set status)."""
        return await self.repository.cancel_task(task_id)
