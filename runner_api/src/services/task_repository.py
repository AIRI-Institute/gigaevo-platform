#!/usr/bin/env python3

import json
import logging
from datetime import datetime, timezone
from typing import Any, List, Optional
from uuid import UUID

from ..models.task import Task, TaskCreate, TaskStatus, TaskType, TaskUpdate
from .redis_client import get_redis

logger = logging.getLogger(__name__)


class TaskRepository:
    def _parse_datetime(self, value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def task_to_hash(self, task: Task) -> dict:
        return {
            "id": str(task.id),
            "experiment_id": task.experiment_id,
            "task_type": task.task_type.value,
            "status": task.status.value,
            "parameters": json.dumps(task.parameters),
            "result": json.dumps(task.result) if task.result is not None else "",
            "error_message": task.error_message or "",
            "created_at": task.created_at.isoformat() if task.created_at else "",
            "started_at": task.started_at.isoformat() if task.started_at else "",
            "completed_at": task.completed_at.isoformat() if task.completed_at else "",
            "worker_id": task.worker_id or "",
            "progress": str(task.progress),
        }

    def hash_to_task(self, data: dict) -> Optional[Task]:
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
                created_at=self._parse_datetime(data.get("created_at")) or datetime.now(timezone.utc),
                started_at=self._parse_datetime(data.get("started_at")),
                completed_at=self._parse_datetime(data.get("completed_at")),
                worker_id=data.get("worker_id") or None,
                progress=float(data.get("progress", 0.0)),
            )
        except Exception as exc:
            task_id = data.get("id", "unknown")
            logger.warning(f"Failed to deserialize task {task_id}: {exc}")
            return None

    def add_enqueue_ops(self, pipe: Any, task: Task) -> None:
        task_key = f"task:{task.id}"
        exp_tasks_key = f"experiment:{task.experiment_id}:tasks"
        pipe.hset(task_key, mapping=self.task_to_hash(task))
        pipe.sadd("all_tasks", str(task.id))
        pipe.lpush(exp_tasks_key, str(task.id))
        pipe.lpush("task_queue", str(task.id))

    async def create_task(self, task: TaskCreate) -> Task:
        redis = await get_redis()
        new_task = Task(
            experiment_id=task.experiment_id,
            task_type=task.task_type,
            parameters=task.parameters,
        )

        pipe = redis.pipeline()
        self.add_enqueue_ops(pipe, new_task)
        await pipe.execute()
        return new_task

    async def get_task(self, task_id: UUID) -> Optional[Task]:
        redis = await get_redis()
        data = await redis.hgetall(f"task:{task_id}")
        return self.hash_to_task(data)

    async def is_task_cancelled(self, task_id: str) -> bool:
        redis = await get_redis()
        val = await redis.hget(f"task:{task_id}", "status")
        return val == TaskStatus.CANCELLED.value

    async def update_task(self, task_id: UUID, task_update: TaskUpdate) -> Optional[Task]:
        redis = await get_redis()
        key = f"task:{task_id}"
        existing = await redis.hgetall(key)
        if not existing:
            return None

        updates: dict = {}
        if task_update.status is not None:
            updates["status"] = task_update.status.value
            if task_update.status in (
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.TERMINATED,
                TaskStatus.CANCELLED,
            ):
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
        return self.hash_to_task(data)

    async def list_tasks(self, status: TaskStatus = None, limit: int = 100, offset: int = 0) -> List[Task]:
        redis = await get_redis()
        task_ids = await redis.smembers("all_tasks")
        if not task_ids:
            return []
        pipe = redis.pipeline()
        for task_id in task_ids:
            pipe.hgetall(f"task:{task_id}")
        hashes = await pipe.execute()
        tasks: List[Task] = []
        for data in hashes:
            task = self.hash_to_task(data)
            if not task:
                continue
            if status and task.status != status:
                continue
            tasks.append(task)
        tasks.sort(key=lambda t: t.created_at)
        return tasks[offset : offset + limit]

    async def delete_task(self, task_id: UUID) -> bool:
        redis = await get_redis()
        key = f"task:{task_id}"
        data = await redis.hgetall(key)
        if not data:
            return False
        experiment_id = data.get("experiment_id")

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
        pipe.srem("all_tasks", str(task_id))
        pipe.lrem("task_queue", 0, str(task_id))
        if experiment_id:
            pipe.lrem(f"experiment:{experiment_id}:tasks", 0, str(task_id))
        await pipe.execute()
        return True

    async def cancel_task(self, task_id: UUID) -> bool:
        redis = await get_redis()
        key = f"task:{task_id}"
        data = await redis.hgetall(key)
        if not data:
            return False

        current_status = data.get("status", TaskStatus.QUEUED.value)
        if current_status in (
            TaskStatus.COMPLETED.value,
            TaskStatus.FAILED.value,
            TaskStatus.TERMINATED.value,
            TaskStatus.CANCELLED.value,
        ):
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

    async def claim_next_task(self, timeout: int) -> Optional[Task]:
        redis = await get_redis()
        popped = await redis.brpop("task_queue", timeout=timeout)
        if not popped:
            return None
        _, task_id = popped
        task_key = f"task:{task_id}"
        data = await redis.hgetall(task_key)
        if not data:
            return None
        if data.get("status") == TaskStatus.CANCELLED.value:
            return None
        task = self.hash_to_task(data)
        if not task:
            return None
        return task

    async def persist_task_state(self, task: Task) -> None:
        redis = await get_redis()
        key = f"task:{task.id}"
        await redis.hset(key, mapping=self.task_to_hash(task))
