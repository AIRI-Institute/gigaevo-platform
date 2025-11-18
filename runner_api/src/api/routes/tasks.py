#!/usr/bin/env python3

from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from ...models.task import Task, TaskCreate, TaskStatus, TaskUpdate
from ...services.task_service import TaskService

router = APIRouter()


def get_task_service() -> TaskService:
    # TODO: Implement dependency injection
    return TaskService()


@router.post("/", response_model=Task)
async def create_task(task: TaskCreate, service: TaskService = Depends(get_task_service)):
    """Create a new task"""
    return await service.create_task(task)


@router.get("/", response_model=List[Task])
async def list_tasks(
    status: TaskStatus = None, limit: int = 100, offset: int = 0, service: TaskService = Depends(get_task_service)
):
    """List tasks with optional status filter"""
    return await service.list_tasks(status=status, limit=limit, offset=offset)


@router.get("/{task_id}", response_model=Task)
async def get_task(task_id: UUID, service: TaskService = Depends(get_task_service)):
    """Get task by ID"""
    task = await service.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.put("/{task_id}", response_model=Task)
async def update_task(task_id: UUID, task_update: TaskUpdate, service: TaskService = Depends(get_task_service)):
    """Update task"""
    task = await service.update_task(task_id, task_update)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.delete("/{task_id}")
async def delete_task(task_id: UUID, service: TaskService = Depends(get_task_service)):
    """Delete task"""
    success = await service.delete_task(task_id)
    if not success:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"message": "Task deleted"}


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: UUID, service: TaskService = Depends(get_task_service)):
    """Cancel task execution"""
    success = await service.cancel_task(task_id)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot cancel task")
    return {"message": "Task cancelled"}
