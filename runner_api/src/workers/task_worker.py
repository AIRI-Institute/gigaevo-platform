#!/usr/bin/env python3

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from redis.asyncio import Redis

from ..config import load_config
from ..models.task import Task, TaskStatus, TaskType
from ..models.worker import WorkerStatus
from ..services.gigavolve_service import GigaEvolveService

logger = logging.getLogger(__name__)


class TaskWorker:
    def __init__(self, worker_id: str, name: str):
        self.worker_id = worker_id
        self.name = name
        self.status = WorkerStatus.IDLE
        self.current_task: Optional[Task] = None
        self.gigavolve_service = GigaEvolveService()
        self.config = load_config()
        self._running = False
        self._redis: Optional[Redis] = None

    async def _get_redis(self) -> Redis:
        if self._redis is None:
            self._redis = Redis.from_url(self.config.redis.url, decode_responses=True)
        return self._redis

    async def start(self):
        """Start the worker"""
        self._running = True
        logger.info(f"Worker {self.worker_id} started")
        # Register worker presence and initial heartbeat
        await self._update_worker_status(WorkerStatus.IDLE, current_task_id=None)

        while self._running:
            try:
                # Stop flag check
                if not await self._should_continue_running():
                    break
                # Heartbeat
                await self._heartbeat()
                # Get next task from queue
                task = await self._get_next_task()
                if task:
                    await self._execute_task(task)
                else:
                    # No tasks available, wait
                    await asyncio.sleep(self.config.worker.polling_interval)

            except Exception as e:
                logger.error(f"Worker {self.worker_id} error: {e}")
                await asyncio.sleep(5)

    async def stop(self):
        """Stop the worker"""
        self._running = False
        if self.current_task:
            # TODO: Mark current task as cancelled
            pass
        logger.info(f"Worker {self.worker_id} stopped")

    async def _get_next_task(self) -> Optional[Task]:
        """Get next task from Redis queue"""
        redis = await self._get_redis()
        # Blocking pop from right (acts as queue FIFO if producers LPUSH)
        timeout = max(1, int(self.config.worker.polling_interval))
        logger.debug(f"Worker {self.worker_id} waiting for task from task_queue (timeout={timeout}s)")
        popped = await redis.brpop("task_queue", timeout=timeout)
        if not popped:
            logger.debug(f"Worker {self.worker_id} no task available after {timeout}s timeout")
            return None
        logger.info(f"Worker {self.worker_id} got task from queue: {popped}")
        _, task_id = popped
        task_key = f"task:{task_id}"
        data = await redis.hgetall(task_key)
        if not data:
            return None
        # If task was already cancelled (e.g., experiment stop), skip it
        if data.get("status") == TaskStatus.CANCELLED.value:
            return None
        try:
            task = Task(
                id=task_id,  # pydantic will coerce UUID
                experiment_id=data.get("experiment_id"),
                task_type=data.get("task_type"),
                status=TaskStatus(data.get("status", TaskStatus.QUEUED.value)),
                parameters=json.loads(data.get("parameters", "{}") or "{}"),
                result=json.loads(data.get("result", "") or "null"),
                error_message=data.get("error_message") or None,
                progress=float(data.get("progress", 0.0)),
            )
            return task
        except Exception:
            return None

    async def _execute_task(self, task: Task):
        """Execute a task"""
        logger.info(
            f"Worker {self.worker_id} executing task {task.id} (type={task.task_type}, experiment={task.experiment_id})"
        )
        self.current_task = task
        self.status = WorkerStatus.BUSY

        try:
            # Early cancellation/status check before attempting to run
            if await self._is_task_cancelled(str(task.id)):
                task.status = TaskStatus.CANCELLED
                task.error_message = task.error_message or "Cancelled before execution"
                return

            # Mark RUNNING in model and Redis
            task.status = TaskStatus.RUNNING
            task.started_at = datetime.now(timezone.utc)
            task.worker_id = self.worker_id
            await self._update_task_in_redis(task)
            await self._update_worker_status(WorkerStatus.BUSY, current_task_id=str(task.id))

            # Check once more right after RUNNING update for a late-arriving cancel
            if await self._is_task_cancelled(str(task.id)):
                task.status = TaskStatus.CANCELLED
                task.error_message = task.error_message or "Cancelled before execution"
                return

            if task.task_type == TaskType.CLONE_REPO:
                await self._handle_clone_repo(task)
            elif task.task_type == TaskType.GENERATE_CODE:
                await self._handle_generate_code(task)
            elif task.task_type == TaskType.RUN_EXPERIMENT:
                await self._handle_run_experiment(task)
            elif task.task_type == TaskType.COLLECT_RESULTS:
                await self._handle_collect_results(task)
            else:
                raise ValueError(f"Unknown task type: {task.task_type}")

        except Exception as e:
            task.status = TaskStatus.FAILED
            task.error_message = str(e)
            logger.error(f"Task {task.id} failed: {e}")

        finally:
            # Persist final state
            task.completed_at = datetime.now(timezone.utc)
            if task.status == TaskStatus.COMPLETED:
                task.progress = 100.0
            await self._update_task_in_redis(task)
            self.current_task = None
            self.status = WorkerStatus.IDLE
            # increment tasks completed counter best-effort
            try:
                redis = await self._get_redis()
                await redis.hincrby(f"worker:{self.worker_id}", "total_tasks_completed", 1)
            except Exception:
                pass
            await self._update_worker_status(WorkerStatus.IDLE, current_task_id=None)
            # If deletion after cancel was requested and task is cancelled, remove task hash and references
            try:
                await self._maybe_delete_after_cancel(str(task.id), str(task.experiment_id))
            except Exception:
                pass
            # For RUN_EXPERIMENT tasks, update experiment-level status in Redis based on final task status
            try:
                if task.task_type == TaskType.RUN_EXPERIMENT and task.experiment_id:
                    await self._update_experiment_status_after_run(str(task.experiment_id), task.status)
            except Exception:
                # Best-effort only; failures here should not break worker loop
                pass

    async def _update_task_in_redis(self, task: Task) -> None:
        redis = await self._get_redis()
        key = f"task:{task.id}"
        mapping = {
            "status": task.status.value,
            "started_at": task.started_at.isoformat() if task.started_at else "",
            "completed_at": task.completed_at.isoformat() if task.completed_at else "",
            "worker_id": task.worker_id or "",
            "progress": str(task.progress),
            "result": json.dumps(task.result) if task.result is not None else "",
            "error_message": task.error_message or "",
        }
        await redis.hset(key, mapping=mapping)

    async def _handle_clone_repo(self, task: Task):
        """Handle repository cloning task"""
        success = await self.gigavolve_service.clone_repository()
        if success:
            task.status = TaskStatus.COMPLETED
            task.result = {"success": True}
        else:
            task.status = TaskStatus.FAILED
            task.error_message = "Failed to clone repository"

    async def _handle_generate_code(self, task: Task):
        """Handle code generation task"""
        prompt = task.parameters.get("prompt", "")
        generated_code = await self.gigavolve_service.generate_code_from_llm(prompt)

        if generated_code:
            task.status = TaskStatus.COMPLETED
            task.result = {"code": generated_code}
        else:
            task.status = TaskStatus.FAILED
            task.error_message = "Failed to generate code"

    async def _handle_run_experiment(self, task: Task):
        """Handle experiment execution task"""
        experiment_id = str(task.experiment_id)
        config = task.parameters.get("config", {})
        logger.info(f"Processing RUN_EXPERIMENT task for experiment {experiment_id}")

        async def _cancel_check() -> bool:
            return await self._is_task_cancelled(str(task.id))

        logger.info(f"Calling gigavolve_service.run_experiment for {experiment_id}")
        result = await self.gigavolve_service.run_experiment(experiment_id, config, cancel_check=_cancel_check)
        logger.info(f"run_experiment returned for {experiment_id}: {result}")

        if result and result.get("success", False):
            task.status = TaskStatus.COMPLETED
            task.result = result
        else:
            # If cancelled mid-run, honor CANCELLED status
            err_msg = result.get("error", "Unknown error") if result else "No result"
            if result and str(err_msg).lower().find("cancel") != -1:
                task.status = TaskStatus.CANCELLED
                task.error_message = err_msg
            else:
                task.status = TaskStatus.FAILED
                task.error_message = err_msg

    async def _handle_collect_results(self, task: Task):
        """Handle results collection task"""
        # The periodic analysis loop in main.py handles plot generation and uploads.
        task.status = TaskStatus.COMPLETED
        task.result = {"info": "Results collection handled by analysis loop"}

    async def _should_continue_running(self) -> bool:
        """Check for a stop signal from Redis."""
        redis = await self._get_redis()
        data = await redis.hgetall(f"worker:{self.worker_id}")
        if data.get("stop_requested") == "1":
            await redis.hset(f"worker:{self.worker_id}", "status", WorkerStatus.OFFLINE.value)
            return False
        return True

    async def _heartbeat(self) -> None:
        """Update last heartbeat timestamp in Redis and ensure registration."""
        redis = await self._get_redis()
        key = f"worker:{self.worker_id}"
        await redis.hset(key, "last_heartbeat", datetime.now(timezone.utc).isoformat())
        await redis.sadd("workers", self.worker_id)

    async def _update_worker_status(self, status: WorkerStatus, current_task_id: Optional[str]) -> None:
        """Update worker status/name and current task in Redis."""
        self.status = status
        redis = await self._get_redis()
        key = f"worker:{self.worker_id}"
        mapping = {
            "id": self.worker_id,
            "name": self.name,
            "status": status.value,
            "current_task_id": current_task_id or "",
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
        }
        pipe = redis.pipeline()
        pipe.hset(key, mapping=mapping)
        pipe.sadd("workers", self.worker_id)
        await pipe.execute()

    async def _is_task_cancelled(self, task_id: str) -> bool:
        """Check if task status has been set to CANCELLED in Redis."""
        redis = await self._get_redis()
        val = await redis.hget(f"task:{task_id}", "status")
        return val == TaskStatus.CANCELLED.value

    async def _maybe_delete_after_cancel(self, task_id: str, experiment_id: str) -> None:
        """Delete task hash and references if flagged and status is CANCELLED."""
        redis = await self._get_redis()
        key = f"task:{task_id}"
        data = await redis.hgetall(key)
        if not data:
            return
        if data.get("delete_after_cancel") == "1" and data.get("status") == TaskStatus.CANCELLED.value:
            pipe = redis.pipeline()
            pipe.delete(key)
            if experiment_id:
                pipe.lrem(f"experiment:{experiment_id}:tasks", 0, task_id)
            await pipe.execute()

    async def _update_experiment_status_after_run(self, experiment_id: str, status: TaskStatus) -> None:
        """
        Update experiment:{id}:status hash based on the final RUN_EXPERIMENT task status.
        This keeps runner-side experiment status in sync with task outcome.
        """
        redis = await self._get_redis()
        status_key = f"experiment:{experiment_id}:status"
        now = datetime.now(timezone.utc).isoformat()
        mapping = {}

        if status == TaskStatus.COMPLETED:
            mapping["status"] = "completed"
            mapping["completed_at"] = now
        elif status == TaskStatus.FAILED:
            mapping["status"] = "failed"
            mapping["completed_at"] = now
        elif status == TaskStatus.CANCELLED:
            mapping["status"] = "cancelled"
            mapping["stopped_at"] = now

        if mapping:
            await redis.hset(status_key, mapping=mapping)
