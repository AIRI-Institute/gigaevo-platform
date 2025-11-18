#!/usr/bin/env python3

import json
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from redis.asyncio import Redis

from ..config import load_config
from ..models.task import Task, TaskCreate, TaskStatus, TaskType, TaskUpdate


class TaskService:
    def __init__(self):
        self.config = load_config()
        self._redis: Optional[Redis] = None

    async def _get_redis(self) -> Redis:
        if self._redis is None:
            self._redis = Redis.from_url(self.config.redis.url, decode_responses=True)
        return self._redis

    @staticmethod
    def _task_hash_to_model(data: dict) -> Optional[Task]:
        if not data:
            return None
        try:
            return Task(
                id=UUID(data["id"]),
                experiment_id=str(data["experiment_id"]),
                task_type=TaskType(data["task_type"]),
                status=TaskStatus(data.get("status", TaskStatus.QUEUED.value)),
                parameters=json.loads(data.get("parameters", "{}") or "{}"),
                result=json.loads(data.get("result", "") or "null"),
                error_message=data.get("error_message") or None,
                created_at=datetime.fromisoformat(data.get("created_at"))
                if data.get("created_at")
                else datetime.now(timezone.utc),
                started_at=datetime.fromisoformat(data.get("started_at")) if data.get("started_at") else None,
                completed_at=datetime.fromisoformat(data.get("completed_at")) if data.get("completed_at") else None,
                worker_id=data.get("worker_id") or None,
                progress=float(data.get("progress", 0.0)),
            )
        except Exception:
            return None

    @staticmethod
    def _task_model_to_hash(task: Task) -> dict:
        return {
            "id": str(task.id),
            "experiment_id": task.experiment_id,
            "task_type": task.task_type.value,
            "status": task.status.value,
            "parameters": json.dumps(task.parameters),
            "result": json.dumps(task.result) if task.result is not None else "",
            "error_message": task.error_message or "",
            "created_at": task.created_at.replace(tzinfo=timezone.utc).isoformat(),
            "started_at": task.started_at.replace(tzinfo=timezone.utc).isoformat() if task.started_at else "",
            "completed_at": task.completed_at.replace(tzinfo=timezone.utc).isoformat() if task.completed_at else "",
            "worker_id": task.worker_id or "",
            "progress": str(task.progress),
        }

    async def create_task(self, task: TaskCreate) -> Task:
        """Create a new task and enqueue it"""
        redis = await self._get_redis()
        new_task = Task(
            experiment_id=task.experiment_id,
            task_type=task.task_type,
            parameters=task.parameters,
        )

        task_key = f"task:{new_task.id}"
        exp_tasks_key = f"experiment:{new_task.experiment_id}:tasks"

        pipe = redis.pipeline()
        pipe.hset(task_key, mapping=self._task_model_to_hash(new_task))
        pipe.lpush(exp_tasks_key, str(new_task.id))
        pipe.lpush("task_queue", str(new_task.id))
        await pipe.execute()
        return new_task

    async def list_tasks(self, status: TaskStatus = None, limit: int = 100, offset: int = 0) -> List[Task]:
        """List tasks with optional status filter (best-effort scan)."""
        redis = await self._get_redis()
        # Simple approach: fetch keys and hydrate
        keys = await redis.keys("task:*")
        tasks: List[Task] = []
        if not keys:
            return tasks
        pipe = redis.pipeline()
        for k in keys:
            pipe.hgetall(k)
        hashes = await pipe.execute()
        for data in hashes:
            task = self._task_hash_to_model(data)
            if not task:
                continue
            if status and task.status != status:
                continue
            tasks.append(task)
        # Apply offset/limit in memory
        return tasks[offset : offset + limit]

    async def get_task(self, task_id: UUID) -> Optional[Task]:
        """Get task by ID"""
        redis = await self._get_redis()
        data = await redis.hgetall(f"task:{task_id}")
        return self._task_hash_to_model(data)

    async def update_task(self, task_id: UUID, task_update: TaskUpdate) -> Optional[Task]:
        """Update task fields"""
        redis = await self._get_redis()
        key = f"task:{task_id}"
        existing = await redis.hgetall(key)
        if not existing:
            return None

        # Build updates
        updates: dict = {}
        if task_update.status is not None:
            updates["status"] = task_update.status.value
            if task_update.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
                updates["completed_at"] = datetime.now(timezone.utc).isoformat()
        if task_update.result is not None:
            updates["result"] = json.dumps(task_update.result)
        if task_update.error_message is not None:
            updates["error_message"] = task_update.error_message
        if task_update.progress is not None:
            updates["progress"] = str(task_update.progress)
        if task_update.worker_id is not None:
            updates["worker_id"] = task_update.worker_id

        if updates:
            await redis.hset(key, mapping=updates)

        data = await redis.hgetall(key)
        return self._task_hash_to_model(data)

    async def delete_task(self, task_id: UUID) -> bool:
        """Delete task and remove from queues"""
        redis = await self._get_redis()
        key = f"task:{task_id}"
        data = await redis.hgetall(key)
        if not data:
            return False
        experiment_id = data.get("experiment_id")

        # If task is RUNNING, mark as CANCELLED and flag for cleanup; worker will observe status
        current_status = data.get("status", TaskStatus.QUEUED.value)
        if current_status == TaskStatus.RUNNING.value:
            await redis.hset(
                key,
                mapping={
                    "status": TaskStatus.CANCELLED.value,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "delete_after_cancel": "1",
                },
            )
            return True

        pipe = redis.pipeline()
        pipe.delete(key)
        # Remove from queues (best-effort)
        pipe.lrem("task_queue", 0, str(task_id))
        if experiment_id:
            pipe.lrem(f"experiment:{experiment_id}:tasks", 0, str(task_id))
        await pipe.execute()
        return True

    async def cancel_task(self, task_id: UUID) -> bool:
        """Cancel task execution if possible (remove from queue, set status)."""
        redis = await self._get_redis()
        key = f"task:{task_id}"
        data = await redis.hgetall(key)
        if not data:
            return False

        current_status = data.get("status", TaskStatus.QUEUED.value)
        if current_status in (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value):
            return False

        pipe = redis.pipeline()
        pipe.hset(
            key,
            mapping={
                "status": TaskStatus.CANCELLED.value,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        pipe.lrem("task_queue", 0, str(task_id))
        experiment_id = data.get("experiment_id")
        if experiment_id:
            pipe.lrem(f"experiment:{experiment_id}:tasks", 0, str(task_id))
        await pipe.execute()
        return True
