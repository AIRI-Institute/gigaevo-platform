#!/usr/bin/env python3

import json
from datetime import datetime, timezone
from typing import List, Optional

from redis.asyncio import Redis

from ..config import load_config
from ..models.worker import Worker, WorkerStatus, WorkerUpdate


class WorkerService:
    def __init__(self):
        self.config = load_config()
        self._redis: Optional[Redis] = None

    async def _get_redis(self) -> Redis:
        if self._redis is None:
            self._redis = Redis.from_url(self.config.redis.url, decode_responses=True)
        return self._redis

    @staticmethod
    def _hash_to_worker(data: dict) -> Optional[Worker]:
        if not data:
            return None
        try:
            return Worker(
                id=data["id"],
                name=data.get("name", data["id"]),
                status=WorkerStatus(data.get("status", WorkerStatus.OFFLINE.value)),
                current_task_id=data.get("current_task_id") or None,
                capabilities=json.loads(data.get("capabilities", "{}") or "{}"),
                resources=json.loads(data.get("resources", "{}") or "{}"),
                created_at=datetime.fromisoformat(data.get("created_at"))
                if data.get("created_at")
                else datetime.now(timezone.utc),
                last_heartbeat=datetime.fromisoformat(data.get("last_heartbeat"))
                if data.get("last_heartbeat")
                else None,
                total_tasks_completed=int(data.get("total_tasks_completed", 0)),
                error_message=data.get("error_message") or None,
            )
        except Exception:
            return None

    @staticmethod
    def _worker_to_hash(worker: Worker) -> dict:
        return {
            "id": worker.id,
            "name": worker.name,
            "status": worker.status.value,
            "current_task_id": worker.current_task_id or "",
            "capabilities": json.dumps(worker.capabilities),
            "resources": json.dumps(worker.resources),
            "created_at": worker.created_at.replace(tzinfo=timezone.utc).isoformat(),
            "last_heartbeat": worker.last_heartbeat.replace(tzinfo=timezone.utc).isoformat()
            if worker.last_heartbeat
            else "",
            "total_tasks_completed": str(worker.total_tasks_completed),
            "error_message": worker.error_message or "",
        }

    async def list_workers(self) -> List[Worker]:
        """List all workers registered in Redis"""
        redis = await self._get_redis()
        worker_ids = await redis.smembers("workers")
        if not worker_ids:
            return []
        pipe = redis.pipeline()
        for wid in worker_ids:
            pipe.hgetall(f"worker:{wid}")
        hashes = await pipe.execute()
        workers: List[Worker] = []
        for data in hashes:
            w = self._hash_to_worker(data)
            if w:
                workers.append(w)
        return workers

    async def get_worker(self, worker_id: str) -> Optional[Worker]:
        """Get worker by ID"""
        redis = await self._get_redis()
        data = await redis.hgetall(f"worker:{worker_id}")
        return self._hash_to_worker(data)

    async def update_heartbeat(self, worker_id: str) -> bool:
        """Update or create worker heartbeat"""
        redis = await self._get_redis()
        key = f"worker:{worker_id}"
        now = datetime.now(timezone.utc).isoformat()
        pipe = redis.pipeline()
        pipe.hset(
            key,
            mapping={
                "id": worker_id,
                "name": worker_id,
                "status": WorkerStatus.IDLE.value,
                "last_heartbeat": now,
                "created_at": now,
            },
        )
        pipe.sadd("workers", worker_id)
        await pipe.execute()
        return True

    async def update_worker(self, worker_id: str, worker_update: WorkerUpdate) -> Optional[Worker]:
        """Update worker status/fields"""
        redis = await self._get_redis()
        key = f"worker:{worker_id}"
        existing = await redis.hgetall(key)
        if not existing:
            return None

        updates: dict = {}
        if worker_update.status is not None:
            updates["status"] = worker_update.status.value
        if worker_update.current_task_id is not None:
            updates["current_task_id"] = worker_update.current_task_id or ""
        if worker_update.resources is not None:
            updates["resources"] = json.dumps(worker_update.resources)
        if worker_update.error_message is not None:
            updates["error_message"] = worker_update.error_message or ""
        if worker_update.total_tasks_completed is not None:
            updates["total_tasks_completed"] = str(worker_update.total_tasks_completed)
        updates["last_heartbeat"] = datetime.now(timezone.utc).isoformat()

        if updates:
            await redis.hset(key, mapping=updates)

        data = await redis.hgetall(key)
        return self._hash_to_worker(data)

    async def stop_worker(self, worker_id: str) -> bool:
        """Signal a worker to stop by setting a stop flag."""
        redis = await self._get_redis()
        key = f"worker:{worker_id}"
        exists = await redis.exists(key)
        if not exists:
            return False
        # Best-effort control flag checked by worker loop
        await redis.hset(key, "stop_requested", "1")
        return True
